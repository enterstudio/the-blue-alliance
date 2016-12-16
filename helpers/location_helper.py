import json
import logging
import re
import tba_config
import urllib

from difflib import SequenceMatcher
from google.appengine.api import memcache, urlfetch
from google.appengine.ext import ndb

from models.location import Location
from models.sitevar import Sitevar
from models.team import Team

GOOGLE_SECRETS = Sitevar.get_by_id("google.secrets")
GOOGLE_API_KEY = None
if GOOGLE_SECRETS is None:
    logging.warning("Missing sitevar: google.api_key. API calls rate limited by IP and may be over rate limit.")
else:
    GOOGLE_API_KEY = GOOGLE_SECRETS.contents['api_key']


class LocationHelper(object):
    @classmethod
    def get_similarity(cls, a, b):
        """
        Returns similarity between two strings from 0 to 1.
        Ignores case and order
        """
        a_sorted = ' '.join(sorted(re.split('\W+', a)))
        b_sorted = ' '.join(sorted(re.split('\W+', b)))
        return SequenceMatcher(None, a_sorted.lower(), b_sorted.lower()).ratio()

    # @classmethod
    # def get_event_lat_lon(cls, event):
    #     """
    #     Try different combinations of venue, address, and location to
    #     get latitude and longitude for an event
    #     """
    #     lat_lon, _ = cls.get_lat_lon(event.venue_address_safe)
    #     if not lat_lon:
    #         lat_lon, _ = cls.get_lat_lon(u'{} {}'.format(event.venue, event.location))
    #     if not lat_lon:
    #         lat_lon, _ = cls.get_lat_lon(event.location)
    #     if not lat_lon:
    #         lat_lon, _ = cls.get_lat_lon(u'{} {}'.format(event.city, event.country))
    #     if not lat_lon:
    #         logging.warning("Finding Lat/Lon for event {} failed!".format(event.key_name))
    #     return lat_lon
    @classmethod
    def get_event_location_info(cls, event):
        """
        Search for different combinations of venue, venue_address, city,
        state_prov, postalcode, and country in attempt to find the correct
        location associated with the event.
        """
        if not event.location:
            return {}

        # Possible queries for location that will match yield results
        if event.venue:
            possible_queries = [event.venue]
        else:
            possible_queries = []
        if event.venue_address:
            split_address = event.venue_address.split('\n')
            for i in xrange(min(len(split_address), 2)):  # Venue takes up at most 2 lines
                query = ' '.join(split_address[0:i+1])  # From the front
                if query not in possible_queries:
                    possible_queries.append(query)

            for i in xrange(len(split_address)):
                query = ' '.join(split_address[i:])  # From the back
                if query not in possible_queries:
                    possible_queries.append(query)

        # Try to find place based on possible queries
        best_score = 0
        best_location_info = {}
        textsearch_results_candidates = []  # More trustworthy candidates are added first
        for query in possible_queries:
            textsearch_results = cls.google_maps_textsearch_async(query).get_result()
            if textsearch_results:
                if len(textsearch_results) == 1:
                    location_info = cls.construct_location_info_async(textsearch_results[0]).get_result()
                    score = cls.compute_event_location_score(event, location_info)
                    if score == 1:
                        # Very likely to be correct if only 1 result and as a perfect score
                        return location_info
                    elif score > best_score:
                        # Only 1 result but score is imperfect
                        best_score = score
                        best_location_info = location_info
                else:
                    # Save queries with multiple results for later evaluation
                    textsearch_results_candidates.append(textsearch_results)

        # Consider all candidates and find best one
        for textsearch_results in textsearch_results_candidates:
            for textsearch_result in textsearch_results:
                location_info = cls.construct_location_info_async(textsearch_result).get_result()
                score = cls.compute_event_location_score(event, location_info)
                if score == 1:
                    return location_info
                elif score > best_score:
                    best_score = score
                    best_location_info = location_info

        return best_location_info

    @classmethod
    def compute_event_location_score(cls, event, location_info):
        """
        Score for correctness. 1.0 is perfect.
        Not checking for absolute equality in case of existing data errors.
        Check with both long and short names
        """
        max_score = 5.0
        score = 0.0
        if event.country:
            score += max(
                cls.get_similarity(location_info.get('country', ''), event.country),
                cls.get_similarity(location_info.get('country_short', ''), event.country))
        if event.state_prov:
            score += max(
                cls.get_similarity(location_info.get('state_prov', ''), event.state_prov),
                cls.get_similarity(location_info.get('state_prov_short', ''), event.state_prov))
        if event.city:
            score += cls.get_similarity(location_info.get('city', ''), event.city)
        if event.postalcode:
            score += cls.get_similarity(location_info.get('postal_code', ''), event.postalcode)
        if event.venue:
            venue_score = cls.get_similarity(location_info.get('name', ''), event.venue)
            score += venue_score * 3

        return min(1.0, score / max_score)

    @classmethod
    def update_team_location(cls, team):
        location_info = cls.get_team_location_info(team)
        if 'lat' in location_info and 'lng' in location_info:
            lat_lng = ndb.GeoPt(location_info['lat'], location_info['lng'])
        else:
            lat_lng = None
        team.normalized_location = Location(
            name=location_info.get('name', None),
            formatted_address=location_info.get('formatted_address', None),
            lat_lng=lat_lng,
            street_number=location_info.get('street_number', None),
            street=location_info.get('street', None),
            city=location_info.get('city', None),
            state_prov=location_info.get('state_prov', None),
            state_prov_short=location_info.get('state_prov_short', None),
            country=location_info.get('country', None),
            country_short=location_info.get('country_short', None),
            postal_code=location_info.get('postal_code', None),
            place_id=location_info.get('place_id', None),
            place_details=location_info.get('place_details', None),
        )

    @classmethod
    def _log_team_location_score(cls, team_key, score):
        text = "Team {} location score: {}".format(team_key, score)
        if score >= 0.8:
            logging.info(text)
        else:
            logging.warning(text)

    @classmethod
    def get_team_location_info(cls, team):
        """
        Search for different combinations of team name (which should include
        high school or title sponsor) with city, state_prov, postalcode, and country
        in attempt to find the correct location associated with the team.
        """
        if not team.location:
            return {}

        # Find possible schools/title sponsors
        possible_names = []
        MAX_SPLIT = 3  # Filters out long names that are unlikely
        if team.name:
            # Guessing sponsors/school by splitting name by '/' or '&'
            split1 = re.split('&', team.name)
            split2 = re.split('/', team.name)
            if split1 and \
                    split1[-1].count('&') < MAX_SPLIT and split1[-1].count('/') < MAX_SPLIT:
                possible_names.append(split1[-1])
            if split2 and split2[-1] not in possible_names and \
                     split2[-1].count('&') < MAX_SPLIT and split2[-1].count('/') < MAX_SPLIT:
                possible_names.append(split2[-1])
            if split1 and split1[0] not in possible_names and \
                     split1[0].count('&') < MAX_SPLIT and split1[0].count('/') < MAX_SPLIT:
                possible_names.append(split1[0])
            if split2 and split2[0] not in possible_names and \
                     split2[0].count('&') < MAX_SPLIT and split2[0].count('/') < MAX_SPLIT:
                possible_names.append(split2[0])

        # Construct possible queries based on name and location. Order matters.
        possible_queries = []
        for possible_name in possible_names:
            for query in [possible_name, u'{} {}'.format(possible_name, team.location)]:
                possible_queries.append(query)

        # Try to find place based on possible queries
        best_score = 0
        best_location_info = {}
        textsearch_results_candidates = []  # More trustworthy candidates are added first
        for query in possible_queries:
            textsearch_results = cls.google_maps_textsearch_async(query).get_result()
            if textsearch_results:
                if len(textsearch_results) == 1:
                    location_info = cls.construct_location_info_async(textsearch_results[0]).get_result()
                    score = cls.compute_team_location_score(team, query, location_info)
                    if score == 1:
                        # Very likely to be correct if only 1 result and has a perfect score
                        cls._log_team_location_score(team.key.id(), score)
                        return location_info
                    elif score > best_score:
                        # Only 1 result but score is imperfect
                        best_score = score
                        best_location_info = location_info
                else:
                    # Save queries with multiple results for later evaluation
                    textsearch_results_candidates.append((textsearch_results, query))

        # Check if we have found anything reasonable
        if best_location_info and best_score > 0.9:
            cls._log_team_location_score(team.key.id(), best_score)
            return best_location_info

        # Try to find place using only location
        if not textsearch_results_candidates:
            query = team.location
            textsearch_results = cls.google_maps_textsearch_async(query).get_result()
            if textsearch_results:
                textsearch_results_candidates.append((textsearch_results, query))

        # Try to find place using only city, country
        if not textsearch_results_candidates and team.city and team.country:
            query = u'{} {}'.format(team.city, team.country)
            textsearch_results = cls.google_maps_textsearch_async(query).get_result()
            if textsearch_results:
                textsearch_results_candidates.append((textsearch_results, query))

        if not textsearch_results_candidates:
            logging.warning("Finding textsearch results for team {} failed!".format(team.key.id()))
            return {}

        # Consider all candidates and find best one
        for textsearch_results, query in textsearch_results_candidates:
            for textsearch_result in textsearch_results:
                location_info = cls.construct_location_info_async(textsearch_result).get_result()
                score = cls.compute_team_location_score(team, query, location_info)
                if score == 1:
                    cls._log_team_location_score(team.key.id(), score)
                    return location_info
                elif score > best_score:
                    best_score = score
                    best_location_info = location_info

        cls._log_team_location_score(team.key.id(), best_score)
        return best_location_info

    @classmethod
    def compute_team_location_score(cls, team, query, location_info):
        """
        Score for correctness. 1.0 is perfect.
        Not checking for absolute equality in case of existing data errors.
        Check with both long and short names
        """
        SCORE_THRESHOLD = 0.5
        max_score = 5.0
        score = 0.0
        if team.country:
            partial = max(
                cls.get_similarity(location_info.get('country', ''), team.country),
                cls.get_similarity(location_info.get('country_short', ''), team.country))
            score += partial if partial < SCORE_THRESHOLD else 1
        if team.state_prov:
            partial = max(
                cls.get_similarity(location_info.get('state_prov', ''), team.state_prov),
                cls.get_similarity(location_info.get('state_prov_short', ''), team.state_prov))
            score += partial if partial < SCORE_THRESHOLD else 1
        if team.city:
            partial = cls.get_similarity(location_info.get('city', ''), team.city)
            score += partial if partial < SCORE_THRESHOLD else 1
        if team.postalcode:
            partial = cls.get_similarity(location_info.get('postal_code', ''), team.postalcode)
            score += partial if partial < SCORE_THRESHOLD else 1

        partial = cls.get_similarity(location_info.get('name', ''), query)
        score += partial if partial < SCORE_THRESHOLD else 1

        if 'point_of_interest' not in location_info.get('types', ''):
            score *= 0.5

        return min(1.0, score / max_score)

    @classmethod
    @ndb.tasklet
    def construct_location_info_async(cls, textsearch_result):
        """
        Gets location info given a textsearch result
        """
        location_info = {
            'place_id': textsearch_result['place_id'],
            'formatted_address': textsearch_result['formatted_address'],
            'lat': textsearch_result['geometry']['location']['lat'],
            'lng': textsearch_result['geometry']['location']['lng'],
            'name': textsearch_result['name'],
            'types': textsearch_result['types'],
        }
        place_details_result = yield cls.google_maps_place_details_async(textsearch_result['place_id'])
        if place_details_result:
            for component in place_details_result['address_components']:
                if 'street_number' in component['types']:
                    location_info['street_number'] = component['long_name']
                elif 'route' in component['types']:
                    location_info['street'] = component['long_name']
                elif 'locality' in component['types']:
                    location_info['city'] = component['long_name']
                elif 'administrative_area_level_1' in component['types']:
                    location_info['state_prov'] = component['long_name']
                    location_info['state_prov_short'] = component['short_name']
                elif 'country' in component['types']:
                    location_info['country'] = component['long_name']
                    location_info['country_short'] = component['short_name']
                elif 'postal_code' in component['types']:
                    location_info['postal_code'] = component['long_name']
            location_info['place_details'] = place_details_result

        raise ndb.Return(location_info)

    @classmethod
    @ndb.tasklet
    def google_maps_textsearch_async(cls, query):
        """
        https://developers.google.com/places/web-service/search#TextSearchRequests
        """
        if not GOOGLE_API_KEY:
            logging.warning("Must have sitevar google.api_key to use Google Maps Textsearch")
            raise ndb.Return(None)

        results = None
        if query:
            cache_key = u'google_maps_textsearch:{}'.format(query.encode('ascii', 'ignore'))
            query = query.encode('utf-8')
            results = memcache.get(cache_key)
            if not results:
                textsearch_params = {
                    'query': query,
                    'key': GOOGLE_API_KEY,
                }
                textsearch_url = 'https://maps.googleapis.com/maps/api/place/textsearch/json?%s' % urllib.urlencode(textsearch_params)
                try:
                    # Make async urlfetch call
                    context = ndb.get_context()
                    textsearch_result = yield context.urlfetch(textsearch_url)

                    # Parse urlfetch result
                    if textsearch_result.status_code == 200:
                        textsearch_dict = json.loads(textsearch_result.content)
                        if textsearch_dict['status'] == 'ZERO_RESULTS':
                            logging.info('No textsearch results for query: {}'.format(query))
                        elif textsearch_dict['status'] == 'OK':
                            results = textsearch_dict['results']
                        else:
                            logging.warning('Textsearch failed!')
                            logging.warning(textsearch_dict)
                    else:
                        logging.warning('Textsearch failed with url {}.'.format(textsearch_url))
                        logging.warning(textsearch_dict)
                except Exception, e:
                    logging.warning('urlfetch for textsearch request failed with url {}.'.format(textsearch_url))
                    logging.warning(e)

                if tba_config.CONFIG['memcache']:
                    memcache.set(cache_key, results)
        raise ndb.Return(results)

    @classmethod
    @ndb.tasklet
    def google_maps_place_details_async(cls, place_id):
        """
        https://developers.google.com/places/web-service/details#PlaceDetailsRequests
        """
        if not GOOGLE_API_KEY:
            logging.warning("Must have sitevar google.api_key to use Google Maps PlaceDetails")
            raise ndb.Return(None)

        result = None
        cache_key = u'google_maps_place_details:{}'.format(place_id)
        result = memcache.get(cache_key)
        if not result:
            place_details_params = {
                'placeid': place_id,
                'key': GOOGLE_API_KEY,
            }
            place_details_url = 'https://maps.googleapis.com/maps/api/place/details/json?%s' % urllib.urlencode(place_details_params)
            try:
                # Make async urlfetch call
                context = ndb.get_context()
                place_details_result = yield context.urlfetch(place_details_url)

                # Parse urlfetch call
                if place_details_result.status_code == 200:
                    place_details_dict = json.loads(place_details_result.content)
                    if place_details_dict['status'] == 'ZERO_RESULTS':
                        logging.info('No place_details result for place_id: {}'.format(place_id))
                    elif place_details_dict['status'] == 'OK':
                        result = place_details_dict['result']
                    else:
                        logging.warning('Placedetails failed!')
                        logging.warning(place_details_dict)
                else:
                    logging.warning('Placedetails failed with url {}.'.format(place_details_url))
            except Exception, e:
                logging.warning('urlfetch for place_details request failed with url {}.'.format(place_details_url))
                logging.warning(e)

            if tba_config.CONFIG['memcache']:
                memcache.set(cache_key, result)

        raise ndb.Return(result)

    @classmethod
    def get_timezone_id(cls, location, lat_lon=None):
        if lat_lon is None:
            result, _ = cls.get_lat_lon(location)
            if result is None:
                return None
            else:
                lat, lng = result
        else:
            lat, lng = lat_lon

        google_secrets = Sitevar.get_by_id("google.secrets")
        google_api_key = None
        if google_secrets is None:
            logging.warning("Missing sitevar: google.api_key. API calls rate limited by IP and may be over rate limit.")
        else:
            google_api_key = google_secrets.contents['api_key']

        # timezone request
        tz_params = {
            'location': '%s,%s' % (lat, lng),
            'timestamp': 0,  # we only care about timeZoneId, which doesn't depend on timestamp
            'sensor': 'false',
        }
        if google_api_key is not None:
            tz_params['key'] = google_api_key
        tz_url = 'https://maps.googleapis.com/maps/api/timezone/json?%s' % urllib.urlencode(tz_params)
        try:
            tz_result = urlfetch.fetch(tz_url)
        except Exception, e:
            logging.warning('urlfetch for timezone request failed: {}'.format(tz_url))
            logging.info(e)
            return None
        if tz_result.status_code != 200:
            logging.warning('TZ lookup for (lat, lng) failed! ({}, {})'.format(lat, lng))
            return None
        tz_dict = json.loads(tz_result.content)
        if 'timeZoneId' not in tz_dict:
            logging.warning('No timeZoneId for (lat, lng)'.format(lat, lng))
            return None
        return tz_dict['timeZoneId']
