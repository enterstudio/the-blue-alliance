import json
import logging
import re
import tba_config
import urllib

from difflib import SequenceMatcher
from google.appengine.api import memcache, urlfetch
from google.appengine.ext import ndb

from helpers.search_helper import SearchHelper
from models.location import Location
from models.sitevar import Sitevar
from models.team import Team


class LocationHelper(object):
    GOOGLE_API_KEY = None

    @classmethod
    def get_similarity(cls, a, b):
        """
        Returns max(similarity between two strings ignoring case,
                    similarity between two strings ignoring case and order,
                    similarity between acronym(a) & b,
                    similarity between a & acronym(b)) from 0 to 1
        where acronym() is generated by splitting along non word characters
        Ignores case and order
        """
        a = a.lower().strip()
        b = b.lower().strip()

        a_split = filter(lambda x: x, re.split('\s+|,|-', a))
        b_split = filter(lambda x: x, re.split('\s+|,|-', b))
        a_sorted = ' '.join(sorted(a_split))
        b_sorted = ' '.join(sorted(b_split))
        a_acr =  ''.join([w[0] if w else '' for w in a_split]).lower()
        b_acr =  ''.join([w[0] if w else '' for w in b_split]).lower()

        sm1 = SequenceMatcher(None, a, b)
        sm2 = SequenceMatcher(None, a_sorted, b_sorted)
        sm3 = SequenceMatcher(None, a_acr, b)
        sm4 = SequenceMatcher(None, a, b_acr)

        return  max([
            sm1.ratio(),
            sm2.ratio(),
            sm3.ratio(),
            sm4.ratio(),
        ])

    @classmethod
    def update_event_location(cls, event):
        if not event.location:
            return

        location_info, score = cls.get_event_location_info(event)

        # Log performance
        text = "Event {} location score: {}".format(event.key.id(), score)
        if score < 0.8:
            logging.warning(text)
        else:
            logging.info(text)

        # Fallback to location only
        if not location_info:
            logging.warning("Falling back to location only for event {}".format(event.key.id()))
            geocode_result = cls.google_maps_geocode_async(event.location).get_result()
            if geocode_result:
                location_info = cls.construct_location_info_async(geocode_result[0]).get_result()
            else:
                logging.warning("Event {} location failed!".format(event.key.id()))

        # Update event
        if 'lat' in location_info and 'lng' in location_info:
            lat_lng = ndb.GeoPt(location_info['lat'], location_info['lng'])
        else:
            lat_lng = None
        event.normalized_location = Location(
            name=location_info.get('name'),
            formatted_address=location_info.get('formatted_address'),
            lat_lng=lat_lng,
            street_number=location_info.get('street_number'),
            street=location_info.get('street'),
            city=location_info.get('city'),
            state_prov=location_info.get('state_prov'),
            state_prov_short=location_info.get('state_prov_short'),
            country=location_info.get('country'),
            country_short=location_info.get('country_short'),
            postal_code=location_info.get('postal_code'),
            place_id=location_info.get('place_id'),
            place_details=location_info.get('place_details'),
        )
        SearchHelper.add_event_location_index(event)

    @classmethod
    def get_event_location_info(cls, event):
        """
        Search for different combinations of venue, venue_address, city,
        state_prov, postalcode, and country in attempt to find the correct
        location associated with the event.
        """
        # Possible queries for location that will match yield results
        if event.venue:
            possible_queries = [event.venue]
        else:
            possible_queries = []

        if event.venue_address:
            split_address = event.venue_address.split('\n')
            # Venue takes up at most 2 lines. Isolate address
            possible_queries.append(' '.join(split_address[1:]))
            possible_queries.append(' '.join(split_address[2:]))

        # Geocode for lat/lng
        lat_lng = cls.get_lat_lng(event.location)
        if not lat_lng:
            return {}, 0

        # Try to find place based on possible queries
        best_score = 0
        best_location_info = {}
        nearbysearch_results_candidates = []  # More trustworthy candidates are added first
        for j, query in enumerate(possible_queries):
            # Try both searches
            nearbysearch_places =  cls.google_maps_placesearch_async(query, lat_lng)
            textsearch_places = cls.google_maps_placesearch_async(query, lat_lng, textsearch=True)

            for results_future in [nearbysearch_places, textsearch_places]:
                for i, place in enumerate(results_future.get_result()[:5]):
                    location_info = cls.construct_location_info_async(place).get_result()
                    score = cls.compute_event_location_score(query, location_info)
                    score *= pow(0.7, j) * pow(0.7, i)  # discount by ranking
                    if score == 1:
                        return location_info, score
                    elif score > best_score:
                        best_location_info = location_info
                        best_score = score

        return best_location_info, best_score

    @classmethod
    def compute_event_location_score(cls, query_name, location_info):
        """
        Score for correctness. 1.0 is perfect.
        Not checking for absolute equality in case of existing data errors.
        """

        if {'point_of_interest', 'premise'}.intersection(set(location_info.get('types', ''))):
            score = pow(max(
                cls.get_similarity(query_name, location_info['name']),
                cls.get_similarity(query_name, location_info['formatted_address'])), 1.0/3)
        else:
            score = 0

        return score

    @classmethod
    def update_team_location(cls, team):
        if not team.location:
            return

        # Try with and without textsearch, pick best
        location_info, score = cls.get_team_location_info(team)
        if score < 0.7:
            logging.warning("Using textsearch for {}".format(team.key.id()))
            location_info2, score2 = cls.get_team_location_info(team, textsearch=True)
            if score2 > score:
                location_info = location_info2
                score = score2

        # Log performance
        text = "Team {} location score: {}".format(team.key.id(), score)
        if score < 0.8:
            logging.warning(text)
        else:
            logging.info(text)

        # Don't trust anything below a certain threshold
        if score < 0.7:
            logging.warning("Location score too low for team {}".format(team.key.id()))
            location_info = {}

        # Fallback to location only
        if not location_info:
            logging.warning("Falling back to location only for team {}".format(team.key.id()))
            geocode_result = cls.google_maps_geocode_async(team.location).get_result()
            if geocode_result:
                location_info = cls.construct_location_info_async(geocode_result[0]).get_result()

        # Fallback to city, country
        if not location_info:
            logging.warning("Falling back to city/country only for team {}".format(team.key.id()))
            city_country = '{} {}'.format(
                team.city if team.city else '',
                team.country if team.country else '')
            geocode_result = cls.google_maps_geocode_async(city_country).get_result()
            if geocode_result:
                location_info = cls.construct_location_info_async(geocode_result[0]).get_result()
            else:
                logging.warning("Team {} location failed!".format(team.key.id()))

        # Update team
        if 'lat' in location_info and 'lng' in location_info:
            lat_lng = ndb.GeoPt(location_info['lat'], location_info['lng'])
        else:
            lat_lng = None
        team.normalized_location = Location(
            name=location_info.get('name'),
            formatted_address=location_info.get('formatted_address'),
            lat_lng=lat_lng,
            street_number=location_info.get('street_number'),
            street=location_info.get('street'),
            city=location_info.get('city'),
            state_prov=location_info.get('state_prov'),
            state_prov_short=location_info.get('state_prov_short'),
            country=location_info.get('country'),
            country_short=location_info.get('country_short'),
            postal_code=location_info.get('postal_code'),
            place_id=location_info.get('place_id'),
            place_details=location_info.get('place_details'),
        )
        SearchHelper.add_team_location_index(team)

    @classmethod
    def get_team_location_info(cls, team, textsearch=False):
        """
        Search for different combinations of team name (which should include
        high school or title sponsor) with city, state_prov, postalcode, and country
        in attempt to find the correct location associated with the team.
        """
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

        # Geocode for lat/lng
        lat_lng = cls.get_lat_lng(team.location)
        if not lat_lng:
            return {}, 0

        # Try to find place based on possible queries
        best_score = 0
        best_location_info = {}
        nearbysearch_results_candidates = []  # More trustworthy candidates are added first
        for name in possible_names:
            places =  cls.google_maps_placesearch_async(name, lat_lng, textsearch=textsearch).get_result()
            for i, place in enumerate(places[:5]):
                location_info = cls.construct_location_info_async(place).get_result()
                score = cls.compute_team_location_score(name, location_info)
                score *= pow(0.7, i)  # discount by ranking
                if score == 1:
                    return location_info, score
                elif score > best_score:
                    best_location_info = location_info
                    best_score = score

        return best_location_info, best_score

    @classmethod
    def compute_team_location_score(cls, query_name, location_info):
        """
        Score for correctness. 1.0 is perfect.
        Not checking for absolute equality in case of existing data errors.
        """
        score = pow(cls.get_similarity(query_name, location_info['name']), 1.0/3)
        if not {'school', 'university'}.intersection(set(location_info.get('types', ''))):
            score *= 0.7

        return score

    @classmethod
    @ndb.tasklet
    def construct_location_info_async(cls, gmaps_result):
        """
        Gets location info given a gmaps result
        """
        location_info = {
            'place_id': gmaps_result['place_id'],
            'lat': gmaps_result['geometry']['location']['lat'],
            'lng': gmaps_result['geometry']['location']['lng'],
            'name': gmaps_result.get('name'),
            'types': gmaps_result['types'],
        }
        place_details_result = yield cls.google_maps_place_details_async(gmaps_result['place_id'])
        if place_details_result:
            has_city = False
            for component in place_details_result['address_components']:
                if 'street_number' in component['types']:
                    location_info['street_number'] = component['long_name']
                elif 'route' in component['types']:
                    location_info['street'] = component['long_name']
                elif 'locality' in component['types']:
                    location_info['city'] = component['long_name']
                    has_city = True
                elif 'administrative_area_level_1' in component['types']:
                    location_info['state_prov'] = component['long_name']
                    location_info['state_prov_short'] = component['short_name']
                elif 'country' in component['types']:
                    location_info['country'] = component['long_name']
                    location_info['country_short'] = component['short_name']
                elif 'postal_code' in component['types']:
                    location_info['postal_code'] = component['long_name']

            # Special case for when there is no city
            if not has_city and 'state_prov' in location_info:
                location_info['city'] = location_info['state_prov']

            location_info['formatted_address'] = place_details_result['formatted_address']

            # Save everything just in case
            location_info['place_details'] = place_details_result

        raise ndb.Return(location_info)

    @classmethod
    @ndb.tasklet
    def google_maps_placesearch_async(cls, query, lat_lng, textsearch=False):
        """
        https://developers.google.com/places/web-service/search#nearbysearchRequests
        https://developers.google.com/places/web-service/search#TextSearchRequests
        """
        if not cls.GOOGLE_API_KEY:
            GOOGLE_SECRETS = Sitevar.get_by_id("google.secrets")
            if GOOGLE_SECRETS:
                cls.GOOGLE_API_KEY = GOOGLE_SECRETS.contents['api_key']
            else:
                logging.warning("Must have sitevar google.api_key to use Google Maps nearbysearch")
                raise ndb.Return([])

        search_type = 'textsearch' if textsearch else 'nearbysearch'

        results = None
        if query:
            query = query.encode('ascii', 'ignore')
            cache_key = u'google_maps_{}:{}'.format(search_type, query)
            results = memcache.get(cache_key)
            if results is None:
                search_params = {
                    'key': cls.GOOGLE_API_KEY,
                    'location': '{},{}'.format(lat_lng[0], lat_lng[1]),
                    'radius': 25000,
                }
                if textsearch:
                    search_params['query'] = query
                else:
                    search_params['keyword'] = query

                search_url = 'https://maps.googleapis.com/maps/api/place/{}/json?{}'.format(search_type, urllib.urlencode(search_params))
                try:
                    # Make async urlfetch call
                    context = ndb.get_context()
                    search_result = yield context.urlfetch(search_url)

                    # Parse urlfetch result
                    if search_result.status_code == 200:
                        search_dict = json.loads(search_result.content)
                        if search_dict['status'] == 'ZERO_RESULTS':
                            logging.info('No {} results for query: {}, lat_lng: {}'.format(search_type, query, lat_lng))
                        elif search_dict['status'] == 'OK':
                            results = search_dict['results']
                        else:
                            logging.warning(u'{} failed with query: {}, lat_lng: {}'.format(search_type, query, lat_lng))
                            logging.warning(search_dict)
                    else:
                        logging.warning(u'{} failed with query: {}, lat_lng: {}'.format(search_type, query, lat_lng))
                        logging.warning(search_dict)
                except Exception, e:
                    logging.warning(u'urlfetch for {} request failed with query: {}, lat_lng: {}'.format(search_type, query, lat_lng))
                    logging.warning(e)

                memcache.set(cache_key, results if results else [])

        raise ndb.Return(results if results else [])

    @classmethod
    @ndb.tasklet
    def google_maps_place_details_async(cls, place_id):
        """
        https://developers.google.com/places/web-service/details#PlaceDetailsRequests
        """
        if not cls.GOOGLE_API_KEY:
            GOOGLE_SECRETS = Sitevar.get_by_id("google.secrets")
            if GOOGLE_SECRETS:
                cls.GOOGLE_API_KEY = GOOGLE_SECRETS.contents['api_key']
            else:
                logging.warning("Must have sitevar google.api_key to use Google Maps PlaceDetails")
                raise ndb.Return(None)

        cache_key = u'google_maps_place_details:{}'.format(place_id)
        result = memcache.get(cache_key)
        if result is None:
            place_details_params = {
                'placeid': place_id,
                'key': cls.GOOGLE_API_KEY,
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
                        logging.warning('Placedetails failed with place_id: {}.'.format(place_id))
                        logging.warning(place_details_dict)
                else:
                    logging.warning('Placedetails failed with place_id: {}.'.format(place_id))
            except Exception, e:
                logging.warning('urlfetch for place_details request failed with place_id: {}.'.format(place_id))
                logging.warning(e)

            if tba_config.CONFIG['memcache']:
                memcache.set(cache_key, result)

        raise ndb.Return(result)

    @classmethod
    def get_lat_lng(cls, location):
        results = cls.google_maps_geocode_async(location).get_result()
        if results:
            return results[0]['geometry']['location']['lat'], results[0]['geometry']['location']['lng']
        else:
            return None

    @classmethod
    @ndb.tasklet
    def google_maps_geocode_async(cls, location):
        cache_key = u'google_maps_geocode:{}'.format(location)
        results = memcache.get(cache_key)
        if results is None:
            context = ndb.get_context()

            if not location:
                raise ndb.Return([])

            location = location.encode('utf-8')

            google_secrets = Sitevar.get_by_id("google.secrets")
            google_api_key = None
            if google_secrets is None:
                logging.warning("Missing sitevar: google.api_key. API calls rate limited by IP and may be over rate limit.")
            else:
                google_api_key = google_secrets.contents['api_key']

            geocode_params = {
                'address': location,
                'sensor': 'false',
            }
            if google_api_key:
                geocode_params['key'] = google_api_key
            geocode_url = 'https://maps.googleapis.com/maps/api/geocode/json?%s' % urllib.urlencode(geocode_params)
            try:
                geocode_results = yield context.urlfetch(geocode_url)
                if geocode_results.status_code == 200:
                    geocode_dict = json.loads(geocode_results.content)
                    if geocode_dict['status'] == 'ZERO_RESULTS':
                        logging.info('No geocode results for location: {}'.format(location))
                    elif geocode_dict['status'] == 'OK':
                        results = geocode_dict['results']
                    else:
                        logging.warning('Geocoding failed!')
                        logging.warning(geocode_dict)
                else:
                    logging.warning('Geocoding failed for location {}.'.format(location))
            except Exception, e:
                logging.warning('urlfetch for geocode request failed for location {}.'.format(location))
                logging.warning(e)

            memcache.set(cache_key, results if results else [])

        raise ndb.Return(results if results else [])

    @classmethod
    def get_timezone_id(cls, location, lat_lng=None):
        if lat_lng is None:
            result = cls.get_lat_lng(location)
            if result is None:
                return None
            else:
                lat, lng = result
        else:
            lat, lng = lat_lng.lat, lat_lng.lon

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
