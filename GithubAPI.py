# tools to query the Github API

import json
import requests
import logging

log = logging.getLogger('github')
session = requests.session()

USER_AGENT = b'mattermost.com'

class RESTError(Exception):
    """ Encapsulate an error returned by the Github REST API. """
    def __init__(self, method, path, error, response):
        self.method = method
        self.path = path
        self.error = error
        if 'message' in response:
            self.message = response['message']
        else:
            self.message = response
    def __str__(self):
        return 'While calling %s %s: %s %r' % \
               (self.method, self.path, self.error, self.message)

class GithubAPI(object):
    """ Session for interacting with the Github API.
    Subclasses will implement authentication and methods for a given role. """

    graphql_url = 'https://api.github.com/graphql'
    rest_url = 'https://api.github.com'

    def get_rate_limit_status(self):
        """ Check our rate limit status. See https://developer.github.com/v3/rate_limit/ """
        return self.api_v3('GET', 'rate_limit')['resources']

    def accept_header_list(self):
        """ List of things to accept in the Accept header. """
        return [b'application/vnd.github.machine-man-preview+json',
                b'application/vnd.github.symmetra-preview+json']

    def make_headers(self):
        """ HTTP headers we are going to send. Auth comes from the subclass. """
        ret = {'User-Agent': USER_AGENT,
               'Accept': b', '.join(self.accept_header_list())}
        auth_value = self.make_auth_header() # to be implemented by subclass
        if auth_value:
            ret['Authorization'] = auth_value
        return ret

    def api_v3(self, method, path, params = {}):
        headers = self.make_headers()

        # tell Github we want to use API v3
        headers['Accept'] += b', application/vnd.github.v3+json'

        if params:
            headers['Content-Type'] = b'application/json'
            data = json.dumps(params)
        else:
            data = None

        the_url = self.rest_url+'/'+path

        response_raw = session.request(method, the_url, headers=headers, data=data)
        log.debug('%s %s... HTTP %d' % (method, the_url, response_raw.status_code))

        if response_raw.status_code == 204: # success, but No Content
            return None

        response = response_raw.json()

        if 'errors' in response:
            raise RESTError(method, path, response['errors'][0], response)
        elif response_raw.status_code not in (200, 201, 204):
            raise RESTError(method, path, 'HTTP status %d' % response_raw.status_code, response)
        return response

    def api_v3_paginated(self, method, path, params = {}):
        # see https://developer.github.com/v3/guides/traversing-with-pagination/
        result = self.api_v3(method, path + '?per_page=50')
        if len(result) >= 50:
            raise Exception('XXXXXX need to implement pagination')
        return result

class GithubAPIUnauthenticated(GithubAPI):
    """ Access the Github API without authentication. """
    def make_auth_header(self): return None

class GithubAPIWithAccessToken(GithubAPI):
    """ Access the Github API using an access token for authentication.
    Works for both individuals/organizations and app installations. """

    def __init__(self, access_token):
        self.access_token = access_token

    def make_auth_header(self):
        return b'token %s' % self.access_token.encode('utf-8')
