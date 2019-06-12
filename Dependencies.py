import os
import re
import json
import time
from requirements.requirement import Requirement as PipRequirement # from Python requirements-parser module
import spdx_lookup
from six.moves.urllib.parse import urlparse
import subprocess
import toml
import requests
import logging
from GithubAPI import GithubAPIUnauthenticated, GithubAPIWithAccessToken

log = logging.getLogger('dependencies')

# regexp to detect .vNN suffix on gopkg.in repo dirs
gopkg_in_v_suffix = re.compile(r'\.v.+$')

# valid values for license_text_source
LICENSE_TEXT_SOURCE_INLINE           = 'inline'           # found inside the code itself
LICENSE_TEXT_SOURCE_PROJECT          = 'project'          # found on the project's official website
LICENSE_TEXT_SOURCE_PACKAGE_REGISTRY = 'package-registry' # reported by the package registry
LICENSE_TEXT_SOURCE_SPDX             = 'spdx'             # downloaded from SPDX based on this dependency's SPDX code

# valid values for license_spdx_source
LICENSE_SPDX_SOURCE_TEXT             = 'inferred-from-license-text'        # the inline/project text matches a known SPDX template
LICENSE_SPDX_SOURCE_PROJECT          = 'project'                           # found on the project's official website
LICENSE_SPDX_SOURCE_PACKAGE_REGISTRY = 'package-registry'                  # reported by the package registry
LICENSE_SPDX_SOURCE_GITHUB           = 'github'                            # reported by GitHub for the repo that hosts the code

# filenames that are likely to contain the full license text, at toplevel in a repo
LICENSE_FILENAMES = ('LICENSE', 'LICENSE.txt', 'LICENSE.md', 'LICENCE.md', 'LICENSE.rst', 'LICENSE.markdown', 'license', 'license.txt','License', 'LICENSE-MIT.txt')
NOTICE_FILENAMES = ('NOTICE', 'NOTICE.txt')

# values that can be appended to DependencyInfo.discrepancies
DISCREP_GITHUB_DOESNT_RECOGNIZE = 'Code has a valid license, but the GitHub API does not recognize it'
DISCREP_NO_LICENSE_FILE = 'Code has a valid license, but it\'s somewhere other than a LICENSE file'
DISCREP_NONSTANDARD_LICENSE = 'Code has a valid license, but it is not one recognized by SPDX'
DISCREP_NONSTANDARD_LICENSE_VARIANT = 'Code has a valid license, and should be recognized by SPDX, but varies too much'
DISCREP_PACKAGE_REGISTRY_INCONSISTENT = 'Code has a valid license, but the package registry lists a different one'
DISCREP_LICENSE_TEXT_UNAVAILABLE = 'Code has a valid license, but we don\'t know where to find the original text'
DISCREP_PACKAGE_REGISTRY_NO_REPO = 'Package registry entry is missing a link to the repo URL'
DISCREP_PACKAGE_REGISTRY_NO_AUTHOR = 'Package registry entry does not list an author'
DISCREP_PACKAGE_REGISTRY_NO_DESCRIPTION = 'Package registry entry does not list a description'
DISCREP_GITHUB_NO_DESCRIPTION = 'GitHub repo does not list a description' # after checking the package registry first
DISCREP_PACKAGE_REGISTRY_NO_LICENSE = 'Package registry entry does not list a license'
DISCREP_PACKAGE_REGISTRY_BAD_URL = 'Package registry entry has a bad project or repo URL'

def spdx_lookup_match(text):
    # detect the variant of Apache where it links to the actual license
    if 'Licensed under the Apache License, Version 2.0 (the "License")' in text:
        return spdx_lookup.LicenseMatch(99.0, spdx_lookup.by_id('Apache-2.0'), None)
    # detect variants of the PSF license
    if 'This LICENSE AGREEMENT is between the Python Software Foundation' in text:
        return spdx_lookup.LicenseMatch(99.0, spdx_lookup.by_id('Python-2.0'), None)
    match = spdx_lookup.match(text)
    if not match:
        # if we don't get a hit on the first try, try again with the first block before a newline removed
        if '\n\n' in text:
            match = spdx_lookup.match(text[text.index('\n\n')+1:])
            if match:
                log.debug('second try!')
    return match

class DependencyInfo(object):
    def __init__(self,
                 source_file = None, # file where we found this dependency
                 namespace = None, # "npm", "pypi", etc.
                 name = None,
                 owner = None, # owner name
                 project_url = None, # homepage URL for the upstream project
                 repo_url = None, # code repo for the upstream project. Often at GitHub.
                 description = None,
                 license_spdx = None, # SPDX ID for the license. May be a compound ID like "(a OR b)"
                 license_spdx_source = None, # one of LICENSE_SPDX_SOURCE_* codes above
                 license_text = None, # complete raw text of the license
                 license_text_source = None,  # one of LICENSE_TEXT_SOURCE_* codes above
                 notice_text = '(not checked)', # any NOTICE.txt we are required to show
                 discrepancies = None, # list of problems we found in the upstream data (DISCREP_* codes above)
                 is_modified = None, # (a guess at) whether or not the dependency has been modified from upstream. True/False if known, None if unknown.
                 ):
        self.source_file = source_file
        self.namespace = namespace
        self.name = name
        self.owner = owner
        self.project_url = project_url
        self.repo_url = repo_url
        self.description = description
        self.license_spdx = license_spdx
        self.license_spdx_source = license_spdx_source
        self.license_text = license_text
        self.license_text_source = license_text_source
        self.notice_text = notice_text
        self.is_modified = is_modified

        # list of discrepancies found in the dependency's upstream info
        # e.g. missing or incorrect license info
        if discrepancies is not None:
            self.discrepancies = discrepancies
        else:
            self.discrepancies = []

        self.validate()

    def validate(self):

        # flag to ignore case when the provided license_text doesn't match the provided license_spdx
        tolerate_spdx_mismatch = False

        # recognize special cases
        if (self.namespace, self.name) == ('golang.vendor', 'golang/freetype'):
            self.license_spdx = '(FTL OR GPL-2.0)' # python spdx lib is missing "GPL-2.0-or-later" which applies here
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            self.discrepancies.append(DISCREP_NONSTANDARD_LICENSE)
        elif (self.namespace, self.name) == ('golang.vendor', 'sean-/seed'):
            self.license_spdx = 'MIT'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # it's a hybrid MIT/BSD license - see https://github.com/sean-/seed/blob/master/LICENSE
            self.discrepancies.append(DISCREP_NONSTANDARD_LICENSE)
        elif (self.namespace, self.name) == ('golang.vendor', 'segmentio/backo-go'):
            self.license_spdx = 'MIT'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # license is inside of the README.md
            self.discrepancies.append(DISCREP_NO_LICENSE_FILE)
        elif (self.namespace, self.name) == ('pypi', 'backports.tempfile'):
            self.license_spdx = 'Python-2.0'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # nonstandard variant of the PSF license text - see https://github.com/pjdelport/backports.tempfile/blob/master/LICENSE
            self.discrepancies.append(DISCREP_NONSTANDARD_LICENSE)
        elif (self.namespace, self.name) == ('golang.vendor', 'dgryski/dgoogauth'):
            self.license_spdx = 'Apache-2.0'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # license is inside of the README.md
            self.discrepancies.append(DISCREP_NO_LICENSE_FILE)
        elif (self.namespace, self.name) == ('golang.vendor', 'certifi/gocertifi'):
            self.license_spdx = 'MPL-2.0'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # license is in LICENSE file, but it's a pointer to the source
            self.discrepancies.append(DISCREP_NO_LICENSE_FILE)
        elif (self.namespace, self.name) == ('pypi', 'cryptography'):
            self.license_spdx = '(Apache-2.0 OR BSD-3-Clause)'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # complex hybrid license - see https://github.com/sean-/seed/blob/master/LICENSE
            self.discrepancies.append(DISCREP_NONSTANDARD_LICENSE)
        elif (self.namespace, self.name) == ('pypi', 'idna'):
            self.license_spdx = 'Python-2.0'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # complex hybrid license - see https://github.com/kjd/idna/blob/master/LICENSE.rst
            self.discrepancies.append(DISCREP_NONSTANDARD_LICENSE)
        elif (self.namespace, self.name) == ('pypi', 'incremental'):
            self.license_spdx = 'MIT'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # MIT with a long preamble https://github.com/python-hyper/hyperlink/blob/master/LICENSE
            self.discrepancies.append(DISCREP_NONSTANDARD_LICENSE)
        elif (self.namespace, self.name) == ('npm', 'localforage-observable'):
            self.license_spdx = 'Apache-2.0'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # XXX should we recognize this variant of Apache? https://github.com/localForage/localForage-observable/blob/master/LICENSE
            self.discrepancies.append(DISCREP_NONSTANDARD_LICENSE_VARIANT)
        elif (self.namespace, self.name) == ('npm', 'react-native-document-picker'):
            self.license_spdx = 'MIT'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # inline license is MIT, but it's listed in the package registry as ISC!
            self.discrepancies.append(DISCREP_PACKAGE_REGISTRY_INCONSISTENT)
        elif (self.namespace, self.name) == ('npm', 'react-native-tableview'):
            self.license_spdx = 'BSD-2-Clause'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # inline license is BSD but it's listed in the package registry as ISC!
            self.discrepancies.append(DISCREP_PACKAGE_REGISTRY_INCONSISTENT)
        elif (self.namespace, self.name) == ('npm', 'fuse.js'):
            self.license_spdx = 'Apache-2.0'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # inline license is Apache-2.0 but it's listed in the package registry as Apache
            self.discrepancies.append(DISCREP_PACKAGE_REGISTRY_INCONSISTENT)
        elif (self.namespace, self.name) == ('pypi', 'Brotli'):
            self.license_spdx = 'MIT'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # inline license is MIT but it's listed in the package registry as Apache-2.0
            self.discrepancies.append(DISCREP_PACKAGE_REGISTRY_INCONSISTENT)
        elif (self.namespace, self.name) == ('npm', 'postcss-modules-scope'):
            self.license_spdx = 'MIT'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # inline license is MIT but it's listed in the package registry as ISC
            self.discrepancies.append(DISCREP_PACKAGE_REGISTRY_INCONSISTENT)
        elif (self.namespace, self.name) == ('npm', 'moment-twitter'):
            self.license_spdx = 'MIT'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # inline license is MIT but it's listed in the package registry as BSD-2-Clause
            self.discrepancies.append(DISCREP_PACKAGE_REGISTRY_INCONSISTENT)
        elif (self.namespace, self.name) == ('npm', 'sjcl'):
            self.license_spdx = '(BSD-2-Clause OR GPL-2.0)'
            self.license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT
            # nonstandard combo license - see https://github.com/bitwiseshiftleft/sjcl/blob/master/LICENSE.txt
            self.discrepancies.append(DISCREP_NONSTANDARD_LICENSE)

        # main validation - make sure text and SPDX are present and agree

        # text YES spdx YES
        if (not tolerate_spdx_mismatch) and self.license_text and self.license_spdx:
            # if both are supplied, make sure the text agrees
            match = spdx_lookup_match(self.license_text)
            if match:
                if match.license.id == self.license_spdx or (self.license_spdx.startswith('(') and match.license.id in self.license_spdx):
                    log.debug('SPDX license match confirmed for %r -> %s confidence %.2f' % \
                              (self, match.license.id, match.confidence))
                # don't quibble between BSD variants
                elif match.license.id.startswith('BSD-') and self.license_spdx.startswith('BSD-'):
                    log.debug('SPDX license match confirmed for %r -> %s/%s (BSD fuzzy match) confidence %.2f' % \
                              (self, self.license_spdx, match.license.id, match.confidence))
                else:
                    raise Exception('SPDX license mismatch vs. text for %r (SPDX expected %s but match yields %s)' % \
                                    (self, self.license_spdx, match.license.id))
            else:
                # SPDX lookup failed, but we have both SPDX and text already, so just use them
                pass

        # text NO spdx NO
        elif (not self.license_text) and (not self.license_spdx):
            raise Exception('Cannot determine license text or SPDX for %r' % self)

        # text NO spdx YES
        elif (not self.license_text) and self.license_spdx:
            # fall back to SPDX
            log.debug('Using SPDX license template "%s" for dep %r' % (self.license_spdx, self))
            self.license_text = get_spdx_license_body(self.license_spdx, self.owner)
            self.license_text_source = LICENSE_TEXT_SOURCE_SPDX
            if DISCREP_NO_LICENSE_FILE not in self.discrepancies:
                self.discrepancies.append(DISCREP_LICENSE_TEXT_UNAVAILABLE)

        # text YES spdx NO
        elif self.license_text and (not self.license_spdx):
            # derive the SPDX from the text
            match = spdx_lookup_match(self.license_text)
            if match:
                log.debug('SPDX license match success for %r -> %s confidence %.2f' % \
                          (self, match.license.id, match.confidence))
                self.license_spdx = match.license.id
                self.license_spdx_source = LICENSE_SPDX_SOURCE_TEXT

            else:
                raise Exception('SPDX license match failure for %r' % self)

        # ensure the SPDX is actually valid
        if self.license_spdx:
            if self.license_spdx.startswith('('): # multiple licenses
                id_list = [id for id in self.license_spdx[1:-1].split(' ') if id not in ('AND', 'OR')]
            else:
                id_list = [self.license_spdx]

            for id in id_list:
                if not spdx_lookup.by_id(id):
                    raise Exception('invalid SPDX ID %s for %r' % (id, self))

        if not self.license_spdx:
            raise Exception('no license SPDX for %r' % self)
        if not self.license_text:
            raise Exception('no license text for %r' % self)

        # validate URLs
        for url in (self.project_url, self.repo_url):
            if url:
                problem_string = self.validate_url(url)
                if problem_string:
                    self.discrepancies.append(DISCREP_PACKAGE_REGISTRY_BAD_URL +u': ' + problem_string)

        if self.discrepancies:
            log.debug('Discrepancies found: ' + (', '.join(map(str, self.discrepancies))))

    @classmethod
    def validate_url(cls, url):
        """ Vet a project or repo URL for problems.
        Return a string describing the problem(s) if found, otherwise None. """
        problem_list = []
        parts = urlparse(url)
        if parts.scheme != 'https':
            problem_list.append(u'Scheme is not https://')
        if parts.params or parts.query:
            problem_list.append(u'URL includes parameters or a query string')
        if parts.fragment:
            problem_list.append(u'URL includes a #fragment')
        if problem_list:
            return u', '.join(problem_list)
        return None


    def __repr__(self):
        return '<DependencyInfo "%s":"%s">' % (self.namespace, self.name)

    def to_markdown(self, include_license_spdx = True, include_license_url = False, prefer_short_name = False):
        if '/' in self.name:
            short_name = '/'.join(self.name.split('/')[1:])
        else:
            short_name = self.name

        if prefer_short_name:
            name_first = short_name
            name_second = self.name
        else:
            name_first = self.name
            name_second = short_name

        if include_license_spdx:
            license = ' ' + self.license_spdx
        else:
            license = ''

        if include_license_url:
            license += '\n  * %s' % self.get_license_text_url(compound_ok = True)

        a_modified_version_of = ' a modified version of' if self.is_modified else ''

        return '''## %s

This product contains%s '%s' by %s.

%s

* HOMEPAGE:
  * %s

* LICENSE:%s''' % \
    (name_first, a_modified_version_of, name_second, self.owner, self.description, self.project_url, license)

    def to_body_text(self):
        ret = u''
        if not self.license_text:
            raise Exception('no license text available')
        if self.license_text_source == LICENSE_TEXT_SOURCE_SPDX:
            reason = {LICENSE_SPDX_SOURCE_PROJECT: u'the official project website',
                      LICENSE_SPDX_SOURCE_PACKAGE_REGISTRY: u'the package registry entry for this project',
                      LICENSE_SPDX_SOURCE_GITHUB: u'the GitHub repository for this project'}[self.license_spdx_source]

            ret += u'''Note: An original license file for this dependency is not available. We determined the type of license based on %s. The following text has been prepared using a template from the SPDX Workgroup (https://spdx.org) for this type of license.\n\n''' % reason

        ret += self.license_text.rstrip()

        return ret

    def get_license_text_url(self, compound_ok = False):
        if self.license_spdx:
            return get_spdx_license_url(self.license_spdx, compound_ok = compound_ok)
        else:
            raise Exception('no license_spdx')

class DependenciesModuleConfig:
    GOLANG_SCAN_VENDOR_DIR = 'vendor-dir'
    GOLANG_SCAN_GO_LIST_VENDOR = 'go-list-vendor'

    def __init__(self,
                 start_path = '.',
                 maxdepth = 999,
                 mock_requests = False,
                 github_access_token = None, # this is for read-only queries
                 golang_scan_method = GOLANG_SCAN_VENDOR_DIR,
                 use_gopkg_toml = False,
                 ):
        self.start_path = start_path
        self.maxdepth = maxdepth
        self.mock_requests = mock_requests
        self.github_access_token = github_access_token
        self.golang_scan_method = golang_scan_method
        self.use_gopkg_toml = use_gopkg_toml

def get_spdx_license_url(id, compound_ok = False):
    """ Get URL to an SPDX license. """
    if id.startswith('('):
        if not compound_ok:
            raise Exception('this is a compound license, but compound_ok is not True')
        ret_list = []
        for lic in id[1:-1].split(' '):
            if lic in ('AND', 'OR'):
                ret_list.append(lic)
            else:
                ret_list.append(get_spdx_license_url_one(lic))
        return ' '.join(ret_list)
    else:
        return get_spdx_license_url_one(id)

def get_spdx_license_url_one(id):
    return 'https://spdx.org/licenses/%s.html' % id

def get_spdx_license_body(id, owner):
    """ Get full text of an SPDX license, with template replacements. Accepts compound IDs. """
    if id.startswith('('):
        ret = u''
        for lic in id[1:-1].split(' '):
            if lic == 'AND':
                ret += u'\n\nAND the following license:\n\n'
            elif lic == 'OR':
                ret += u'\n\nOR the following license:\n\n'
            else:
                ret += get_spdx_license_body_one(lic, owner)
        return ret
    else:
        return get_spdx_license_body_one(id, owner)

def get_spdx_license_body_one(id, owner):
    license = spdx_lookup.by_id(id)
    assert license
    this_year = time.strftime('%Y')
    ret = license.template \
           .replace('<<var;name=copyright;original= <year> <owner>;match=.+>>', this_year + ' ' + owner) \
           .replace('[yyyy]', this_year) \
           .replace('<year>', this_year) \
           .replace('<dates>', this_year) \
           .replace('<owner>', owner) \
           .replace('[name of copyright owner]', owner) \
           .replace('<copyright holders>', owner) \
           .replace('<Copyright Holder> (<URL|email>)', owner)

    ret = re.sub(r'<<var;name=.+;original=(.+);match=.+>>', r'\1', ret)
    ret = re.sub(r'<<beginOptional.+<<endOptional>>', '', ret, flags = re.MULTILINE | re.DOTALL)

    if '<<' in ret:
        log.debug(ret)
        raise Exception('incomplete SPDX replacement:\n%s' % ret)

    return ret

# for reaching out to external HTTP APIs
# note that a Github access token is needed to avoid the strict rate limiting on anonymous requests
# (it can be any access token, no write permissions needed)
class ExternalHTTP(object):
    def search_github_for_license_file(self, account, repodir, branch = 'master'):
        return self.search_github_for_file(account, repodir, branch, LICENSE_FILENAMES)
    def search_github_for_notice_file(self, account, repodir, branch = 'master'):
        return self.search_github_for_file(account, repodir, branch, NOTICE_FILENAMES)

    def search_github_for_file(self, account, repodir, branch, filename_list):
        for fname in filename_list:
            url = 'https://raw.githubusercontent.com/%s/%s/%s/%s' % (account, repodir, branch, fname)
            text = self.slurp_url(url, fail_missing = False)
            if text:
                # log.debug('  Found the license file on GitHub: %s' % fname)
                return text

        # hmm, maybe it uses a different default branch?
        if branch == 'master':
            github_repo_data = self.get_github_repo_data('%s/%s' % (account, repodir))
            if github_repo_data['default_branch'] and github_repo_data['default_branch'] != 'master':
                return self.search_github_for_file(account, repodir, github_repo_data['default_branch'], filename_list)

        return None

    def get_github_user_name(self, account):
        """ Return the real name (GitHub 'name' property) of a GitHub user or organization.
        Falls back to the GitHub 'login' property if there is no 'name' property. """
        data = self.get_github_user_data(account)
        if data.get('name') and data['name'] != account:
            return data['name']
        return 'GitHub user "%s"' % account

    def get_github_owner_name(self, repo):
        """ Return the real name of the owner of a GitHub repo.
        If the repo is a fork, also mention the upstream owner. """
        repo_data = self.get_github_repo_data(repo)
        owner = self.get_github_user_name(repo_data['owner']['login'])
        if repo_data['fork']:
            owner += ', modified (forked) from original GitHub repo \'%s\' owned by %s' % \
                     (repo_data['source']['full_name'], self.get_github_user_name(repo_data['source']['owner']['login']))
        return owner


class RealExternalHTTP(ExternalHTTP):
    def __init__(self, github_access_token):
        ExternalHTTP.__init__(self)
        self.spdx_license_body_cache = {}
        self.session = requests.session()
        if github_access_token:
            self.github_api = GithubAPIWithAccessToken(github_access_token)
        else:
            log.warning('Without a github access token, you are very likely to hit rate limits')
            self.github_api = GithubAPIUnauthenticated()

    def get_pypi(self, name):
        return self.session.get('https://pypi.org/pypi/%s/json' % name).json()
    def get_npm(self, name):
        return self.session.get('https://registry.npmjs.org/%s/' % name,
                                headers = {'Accept': b'application/json'}).json()
    def get_github(self, path):
        return self.github_api.api_v3('GET', path)
    def get_github_user_data(self, account):
        return self.get_github('users/%s' % account)
    def get_github_repo_data(self, repo):
        return self.get_github('repos/%s' % repo)
    def slurp_github_license_body(self, key, owner):
        if key == 'other':
            raise Exception('"other" is not a valid Github license key')
        return self.get_github('licenses/%s' % key)['body'].replace('[fullname]', owner).replace('[year]', self.this_year)
    def slurp_url(self, url, fail_missing = True):
        response = self.session.get(url)
        if response.status_code == 200:
            return response.text
        elif response.status_code == 404 and (not fail_missing):
            return None
        raise Exception('%s -> HTTP %d' % (url, response.status_code))

class MockExternalHTTP(ExternalHTTP):
    def get_pypi(self, name):
        return {'info': {'project_url': 'MOCK-PROJECT-URL',
                         'license': 'Apache 2.0',
                         'summary': 'Mocked data for test purposes',
                         'author': 'MOCK-AUTHOR', 'maintainer': 'MOCK-MAINTAINER',
                         'home_page': 'MOCK-HOME-PAGE'}}
    def get_npm(self, name):
        return {'name': name,
                'author': {'name': 'MOCK-AUTHOR'}, 'description': 'Mocked data for test purposes',
                'homepage': 'MOCK-HOME-PAGE', 'license': 'apache-2.0', 'repository': {'url': 'MOCK-REPOSITORY-URL'}}
    def get_github_owner_name(self, repo):
        return 'MOCK-GITHUB-UI-NAME'
    def get_github_repo_data(self, repo):
        return {'description': 'Mock data for test purposes',
                'license': {'name': 'Apache-2.0', 'key': 'apache-2.0', 'spdx_id': 'Apache-2.0', 'url': 'MOCK-LICENSE-URL'}}
    def slurp_github_license_body(self, key, owner):
        return 'MOCK-LICENSE-BODY-TEXT'
    def slurp_url(self, url, fail_missing = True):
        return 'MOCK-URL-CONTENTS'

class DependenciesModule:
    def __init__(self, config):
        self.config = config
        # choose whether to use real or mocked HTTP APIs
        if self.config.mock_requests:
            self.external = MockExternalHTTP()
        else:
            self.external = RealExternalHTTP(self.config.github_access_token)

    def get_dependency_info(self):
        """ Crawl the source code and return a list of DependencyInfo objects. """

        dep_info_list = []

        start_depth = self.config.start_path.count(os.sep)

        # for security, make sure this never looks outside the repo directory
        if '..' in self.config.start_path.split(os.path.sep):
            raise Exception('start_path does not allow ".."')

        log.debug('Starting crawl at cwd %s//%s' % (os.getcwd(), self.config.start_path))

        if self.config.golang_scan_method == DependenciesModuleConfig.GOLANG_SCAN_GO_LIST_VENDOR:
            # run "go list" once at top level
            dep_info_list += self.handle_golang_list_scan(self.config.start_path, self.config.use_gopkg_toml)

        for cur_path, dirs, files in os.walk(self.config.start_path, followlinks=False):
            # prune below maxdepth
            cur_depth = cur_path.count(os.sep) - start_depth
            if cur_depth >= self.config.maxdepth - 1:
                del dirs[:]
            else: # prune uninteresting directories
                dirs[:] = filter(lambda d: not d.startswith('.'), dirs)

            for filename in files:
                handler = None

                if filename == 'requirements.txt':
                    handler = self.handle_python_requirements_txt
                elif filename == 'package.json':
                    handler = self.handle_npm_package_json

                if handler:
                    full_path = os.path.join(cur_path, filename)
                    # path from package root to the file we're handling
                    relative_path = os.sep.join(full_path.split(os.sep)[start_depth+1:])
                    log.debug('Handling %s' % relative_path)
                    dep_info_list += handler(relative_path, open(full_path, 'r'))

            to_remove = []

            for dirname in dirs:
                if dirname == 'vendor' and cur_depth == 0 and self.config.golang_scan_method == DependenciesModuleConfig.GOLANG_SCAN_VENDOR_DIR:
                    # prevent os.walk from recursing below this
                    to_remove.append(dirname)

                    # check for Golang dependencies
                    full_path = os.path.join(cur_path, dirname)
                    relative_path = os.sep.join(full_path.split(os.sep)[start_depth+1:])
                    log.debug('Handling %s' % relative_path)
                    dep_info_list += self.handle_golang_vendor_dir(relative_path, full_path)

                elif dirname == 'node_modules':
                    # do nothing here
                    to_remove.append(dirname)

            for dirname in to_remove:
                dirs.remove(dirname)

        return dep_info_list

    def handle_python_requirements_txt(self, filename, fd):
        """ Parse requirements.txt and perform PyPi lookups. Returns list of DependencyInfo objects. """
        out = {}

        # note: requirements.parse() can open arbitrary files/URLs, so don't use it!
        for line in fd.readlines():
            line = line.strip()
            if not line: continue
            elif line.startswith('#'): continue
            elif line.startswith('-'): # various flags are not  handled
                raise Exception('unhandled line %r' % line)
            elif '#egg=' in line: continue # requirements-parser doesn't handle these well

            req = PipRequirement.parse(line)

            assert req.name not in out

            # req properties:
            # 'name'
            # 'line' - the original line it came from
            # 'specs' - like [('>=','1.2.3'),('<','1.3')]
            # 'editable', 'extras', 'hash', 'hash_name', 'keys', 'local_file',
            # 'path', 'revision', 'specifier', 'subdirectory', 'uri', 'vcs'

            # query PyPi
            data = self.external.get_pypi(req.name)

            # things we care about
            FIELDS = ['info',]

            # get version info?
            # FIELDS.append('releases')

            data = dict((k, v) for k, v in data.items() if k in FIELDS)

            INFO_FIELDS = ['project_urls', 'project_url',
                           'license', 'summary', # 'description',
                           'author', 'maintainer',
                           'home_page']

            # get recursive dependencies?
            # INFO_FIELDS.append('requires_dist')

            if 'info' in data:
                data['info'] = dict((k, v) for k, v in data['info'].items() if k in INFO_FIELDS)

            RELEASE_FIELDS = ['upload_time', 'digests', 'size']
            if 'releases' in data:
                for rel_list in data['releases'].values():
                    for i, rel in enumerate(rel_list[:]):
                        rel_list[i] = dict((k, v) for k, v in rel.items() if k in RELEASE_FIELDS)

            log.debug('PyPi %s -> %r' % (req.name, data))

            license_spdx = None
            license_spdx_source = None
            license_text = None
            license_text_source = None
            notice_text = None
            discrepancies = []

            pypi_license_type = data['info']['license']
            if pypi_license_type:
                if pypi_license_type.startswith('"'): # bad quotes
                    pypi_license_type = pypi_license_type[1:-1]

                # translate PyPi license types to SPDX
                license_spdx = {'Apache License, Version 2.0': 'Apache-2.0',
                                'Apache License 2.0': 'Apache-2.0',
                                'Apache 2.0': 'Apache-2.0',
                                'CC0-1.0': 'CC0-1.0',
                                'MIT': 'MIT',
                                'MIT License': 'MIT',
                                'BSD-2-Clause': 'BSD-2-Clause',

                                # note: "BSD" in PyPi is ambiguous
                                # assume the more restrictive 3-clause license,
                                # and use fuzzy matching when we check it against SPDX.
                                'BSD': 'BSD-3-Clause',
                                'BSD License': 'BSD-3-Clause',
                                'BSD-like': 'BSD-3-Clause',

                                'MPL-2.0': 'MPL-2.0',
                                'MPL2': 'MPL-2.0',
                                'Standard PIL License': 'MIT',
                                'LGPL': 'LGPL-2.1',
                                'BSD or Apache License, Version 2.0': '(BSD-3-Clause OR Apache-2.0)',
                                'Python Software Foundation License': 'Python-2.0',
                                'PSF': 'Python-2.0',
                                'LGPL with exceptions or ZPL': '(LGPL-3.0 OR ZPL-2.1)',
                                'ZPL 2.1': 'ZPL-2.1',
                                }.get(pypi_license_type, None)
                if not license_spdx:
                    raise Exception('no SPDX translation of PyPi license type %r' % pypi_license_type)
                else:
                    license_spdx_source = LICENSE_SPDX_SOURCE_PACKAGE_REGISTRY

            else:
                discrepancies.append(DISCREP_PACKAGE_REGISTRY_NO_LICENSE)

            if '//github.com/' in data['info']['home_page']:
                # query Github for license file
                parts = urlparse(data['info']['home_page'])
                account, repodir = parts.path.split('/')[1:3]
                text = self.external.search_github_for_license_file(account, repodir)
                if text:
                    license_text = text
                    license_text_source = LICENSE_TEXT_SOURCE_INLINE # (github)
                notice_text = self.external.search_github_for_notice_file(account, repodir)

            else:
                # XXXXXX look inside the package for LICENSE/NOTICE
                pass

            out[req.name] = DependencyInfo(source_file = filename,
                                           namespace = 'pypi',
                                           name = req.name,
                                           owner = data['info']['author'],
                                           project_url = data['info']['home_page'],
                                           repo_url = data['info']['project_url'],
                                           description = data['info']['summary'] or (u'A PyPi package named %s' % req.name),
                                           license_spdx = license_spdx,
                                           license_spdx_source = license_spdx_source,
                                           license_text = license_text,
                                           license_text_source = license_text_source,
                                           notice_text = notice_text,
                                           discrepancies = discrepancies,
                                           )

        return sorted(out.values(), key = lambda dep: dep.name)

    def handle_npm_package_json(self, filename, fd):
        """ Parse package.json and perform NPM lookups. Returns list of DependencyInfo objects. """

        data = json.load(fd)
        out = {}

        for depname, depver in data.get('dependencies', {}).items():
            assert depname not in out

            discrepancies = []

            # query NPM

            data = self.external.get_npm(depname)

            # specs at https://github.com/npm/registry/blob/master/docs/responses/package-metadata.md

            assert data['name'] == depname
            del data['name']

            # things we care about
            FIELDS = ['modified', 'author', 'contributors', 'description', 'homepage', 'license', 'repository']
            VERSION_FIELDS = ['name', 'version', 'author', 'contributors', 'description'] # , 'homepage', 'license', 'repository']

            # get version info?
            # FIELDS += ['dist-tags', 'versions']

            if 'dist-tags' in data and 'latest' in data['dist-tags'] and \
               'versions' in data and data['dist-tags']['latest'] in data['versions']:
                latest_version = data['versions'][data['dist-tags']['latest']]
                latest_version = dict((k, v) for k, v in latest_version.items() if k in VERSION_FIELDS)
            else:
                latest_version = None

            data = dict((k, v) for k, v in data.items() if k in FIELDS)

            # get recursive dependencies?
            # note: there are also optionalDependencies, devDependencies, etc. and 'dist'.
            # VERSION_FIELDS.append('dependencies')
            if 'versions' in data:
                for k in data['versions'].keys():
                    data['versions'][k] = dict((i, j) for i, j in data['versions'][k].items() if i in VERSION_FIELDS)

            log.debug('NPM %s -> %r, latest_version %r' % (depname, data, latest_version))

            if 'repository' in data:
                repo_url = data['repository']['url']

                # fix URLs that don't have a scheme
                if repo_url.startswith('git@github.com:'):
                    repo_url = repo_url.replace('git@github.com:', 'https://github.com/')

                # special case for rebound-js
                if repo_url.startswith('https://git@github.com:facebook/'):
                    repo_url = repo_url.replace('https://git@github.com:facebook/', 'https://git@github.com/facebook/')

                # trim off Git-specific parts of the URL and return a clean one that starts with https://
                parts = urlparse(repo_url)
                netloc = parts.netloc

                if '@' in netloc: # remove username
                    netloc = netloc[netloc.index('@')+1:]

                repo_url = 'https://' + netloc + parts.path

                if repo_url.endswith('.git'):
                    repo_url = repo_url[:-4]
            elif depname.startswith('mattermost-'): # special case
                repo_url = 'https://github.com/mattermost/%s' % depname
                discrepancies.append(DISCREP_PACKAGE_REGISTRY_NO_REPO)
            elif depname == 'react-native-cookies': # special case
                repo_url = 'https://github.com/joeferraro/react-native-cookies'
                discrepancies.append(DISCREP_PACKAGE_REGISTRY_NO_REPO)
            elif depname == 'react-native-section-list-get-item-layout': # special case
                repo_url = 'https://github.com/jsoendermann/rn-section-list-get-item-layout'
                discrepancies.append(DISCREP_PACKAGE_REGISTRY_NO_REPO)
            elif depname == 'redux-action-buffer': # special case
                repo_url = 'https://github.com/rt2zz/redux-action-buffer'
                discrepancies.append(DISCREP_PACKAGE_REGISTRY_NO_REPO)
            else:
                raise Exception('unable to determine repo_url for NPM package %r' % depname)

            # some packages do not list an author(!) - try to find it using github
            if 'author' in data and data['author']:
                owner = data['author']['name']
            elif latest_version and 'author' in latest_version and latest_version['author']:
                # some packages only list an author within "versions"
                owner = latest_version['author']['name']
            elif 'contributors' in data and data['contributors']:
                if isinstance(data['contributors'][0], dict):
                    owner = data['contributors'][0]['name']
                else:
                    owner = data['contributors'][0]
            elif latest_version and 'contributors' in latest_version and latest_version['contributors']:
                if isinstance(latest_version['contributors'][0], dict):
                    owner = latest_version['contributors'][0]['name']
                else:
                    owner = latest_version['contributors'][0]
            else:
                discrepancies.append(DISCREP_PACKAGE_REGISTRY_NO_AUTHOR)
                if 'github.com/' in repo_url:
                    parts = urlparse(repo_url)
                    account, repodir = parts.path.split('/')[1:3]
                    owner = self.external.get_github_owner_name('%s/%s' % (account, repodir))
                else:
                    raise Exception('unable to determine owner for NPM package %r' % depname)

            description = None
            if data.get('description'):
                description = data['description']
            elif latest_version and latest_version.get('description'):
                description = latest_version['description']
            else:
                discrepancies.append(DISCREP_PACKAGE_REGISTRY_NO_DESCRIPTION)
                if 'github.com/' in repo_url:
                    parts = urlparse(repo_url)
                    account, repodir = parts.path.split('/')[1:3]
                    github_repo_data = self.external.get_github_repo_data('%s/%s' % (account, repodir))
                    if github_repo_data.get('description'):
                        description = github_repo_data['description']
                    else:
                        discrepancies.append(DISCREP_GITHUB_NO_DESCRIPTION)
                if not description:
                    raise Exception('unable to determine description for NPM package %r' % depname)
                    #description = 'No description is available.'

            license_spdx = None
            license_spdx_source = None
            license_text = None
            license_text_source = None
            license_text_url = None
            notice_text = None

            if 'license' in data:
                # NPM reports a couple of different formats...
                license_spdx_source = LICENSE_SPDX_SOURCE_PACKAGE_REGISTRY

                if isinstance(data['license'], dict):
                    license_spdx = data['license']['type']
                    license_text_url = data['license']['url']
                    license_text_source = LICENSE_TEXT_SOURCE_PACKAGE_REGISTRY
                    # sometimes this is mis-reported as a GitHub HTML link instead of raw
                    if re.match(r'.*github.com/.+/.+/blob/.+', license_text_url):
                        license_text_url = license_text_url.replace('/blob/', '/raw/')
                elif isinstance(data['license'], list):
                    license_spdx = '(' + ' AND '.join(data['license']) + ')'
                else:
                    license_spdx = data['license']

            else:
                discrepancies.append(DISCREP_PACKAGE_REGISTRY_NO_LICENSE)
                if '//github.com/' in repo_url:
                    # uh oh, try to figure out the license using github
                    parts = urlparse(repo_url)
                    account, repodir = parts.path.split('/')[1:3]
                    github_repo_data = self.external.get_github_repo_data('%s/%s' % (account, repodir))
                    log.debug('  Github repo license data %r' % github_repo_data['license'])
                    if github_repo_data['license']['key'] != 'other':
                        license_spdx = github_repo_data['license']['spdx_id']
                        license_spdx_source = LICENSE_SPDX_SOURCE_GITHUB

            if 'homepage' in data:
                project_url = data['homepage']
            elif repo_url:
                project_url = repo_url

            if (not license_text) and license_text_url:
                license_text = self.external.slurp_url(license_text_url)


            if '//github.com/' in repo_url:
                parts = urlparse(repo_url)
                account, repodir = parts.path.split('/')[1:3]

                if repodir.endswith('.git'):
                    repodir = repodir[:-4]

                # try to use inline license text rather than falling back to SPDX
                if not license_text:
                    text = self.external.search_github_for_license_file(account, repodir)
                    if text:
                        license_text = text
                        license_text_source = LICENSE_TEXT_SOURCE_INLINE # (github)

                    # special case
                    if depname == 'twemoji':
                        assert license_spdx == '(MIT AND CC-BY-4.0)'
                        license_text = u'# Code licensed under the MIT License:\n\n' + license_text
                        license_text += u'\n\n# Graphics licensed under CC-BY 4.0:\n\n'
                        license_text += self.external.slurp_url('https://raw.githubusercontent.com/twitter/twemoji/gh-pages/LICENSE-GRAPHICS')

                notice_text = self.external.search_github_for_notice_file(account, repodir)
            else:
                # XXXXXX look inside the package for LICENSE/NOTICE
                pass

            # try to determine if this is a "Modified" version
            is_modified = None

            if depver.startswith('github:'):
                # using a github version like "github:foo/bar#ed33baecd7d7fa9"
                # parse out the repo
                depver_account, depver_repodir = depver.split(':')[1].split('#')[0].split('/')

                # is it the official repo?
                if '//github.com/' in repo_url and (depver_account, depver_repodir) == (account, repodir):
                    pass # yes, it's the official repo
                else:
                    # no, it's a modified repo
                    is_modified = True

            out[depname] = DependencyInfo(source_file = filename,
                                          namespace = 'npm',
                                          name = depname,
                                          owner = owner,
                                          project_url = project_url,
                                          repo_url = repo_url,
                                          description = description,
                                          license_spdx = license_spdx,
                                          license_spdx_source = license_spdx_source,
                                          license_text = license_text,
                                          license_text_source = license_text_source,
                                          notice_text = notice_text,
                                          discrepancies = discrepancies,
                                          is_modified = is_modified,
                                          )

        return sorted(out.values(), key = lambda dep: dep.name)

    def recognize_golang_vendor_dep(self, relative_path, full_path, gopkg_constraint = None):
        """ Given a path that ends like "../github.com/foo/bar"
        return DependencyInfo if it matches a likely Go repo,
        otherwise None. """

        split_path = full_path.split(os.sep)
        if len(split_path) < 2: return None

        if split_path[-2] == 'gopkg.in' and gopkg_in_v_suffix.search(split_path[-1]):
            # special treatment for non-namespaced gopkg.in deps
            host = 'github.com'
            repodir = gopkg_in_v_suffix.sub('', split_path[-1])
            account = 'go-' + repodir

            log.debug('Golang gopkg.in 1st-level dependency %s -> %s/%s/%s' % (full_path, host, account, repodir))
            return self.handle_golang_vendor_dep(relative_path, full_path, host, account, repodir, gopkg_constraint = gopkg_constraint)

        elif split_path[-2] in ('google.golang.org', 'go.uber.org'):
            # special treatment for non-namespaced deps here
            host = split_path[-2]
            account = None
            repodir = split_path[-1]
            dep_name = '%s/%s' % (host, repodir)
            log.debug('Golang %s 1st-level dependency %s -> %s' % (host, full_path, dep_name))
            return self.handle_golang_vendor_dep(relative_path, full_path, host, account, repodir, gopkg_constraint = gopkg_constraint)

        elif len(split_path) >= 3:
            host = split_path[-3] # "github.com"
            if '.' not in host: # apparently not a domain name
                return None
            account = split_path[-2] # "foo"
            repodir = split_path[-1] # "bar"

            # gopkg.in redirects to github
            if host == 'gopkg.in':
                host = 'github.com'
                # but strip a .vx suffix
                repodir = gopkg_in_v_suffix.sub('', repodir)

            dep_name = '%s/%s' % (account, repodir)
            log.debug('Golang vendor dependency %s -> %s/%s/%s' % (full_path, host, account, repodir))
            return self.handle_golang_vendor_dep(relative_path, full_path, host, account, repodir, gopkg_constraint = gopkg_constraint)

        return None

    def handle_golang_vendor_dir(self, relative_path, start_path):
        out = {}

        start_depth = start_path.count(os.sep)
        for cur_path, dirs, files in os.walk(start_path, followlinks=False):
            cur_depth = cur_path.count(os.sep) - start_depth

            if cur_depth == 0:
                # now at "github.com"
                pass
            elif cur_depth == 1:
                # we are now seeing dirs like "gopkg.in/bar"
                to_remove = []
                for dirname in dirs:
                    full_path = os.path.join(cur_path, dirname)
                    dep = self.recognize_golang_vendor_dep(relative_path, full_path)
                    if dep:
                        out[dep.name] = dep
                        # don't recurse below this
                        to_remove.append(dirname)

                for dirname in to_remove:
                    dirs.remove(dirname)

            elif cur_depth == 2:
                # we are now seeing dirs like "github.com/foo/bar"
                for dirname in dirs:
                    full_path = os.path.join(cur_path, dirname)
                    dep = self.recognize_golang_vendor_dep(relative_path, full_path)
                    if dep:
                        out[dep.name] = dep

            elif cur_depth > 2:
                del dirs[:] # don't recurse below depth 2

        return sorted(out.values(), key = lambda dep: dep.name)

    def handle_golang_vendor_dep(self, source_file, full_path, host, account, repodir,
                                 gopkg_constraint = None):
        if account:
            dep_name = '%s/%s' % (account, repodir)
        else:
            dep_name = repodir

        github_account = account
        license_spdx = None
        license_spdx_source = None
        license_text = None
        license_text_source = None
        license_text_url = None
        notice_text = None
        is_modified = None
        discrepancies = []

        # query the host for owner info
        if host == 'github.com':
            project_url = repo_url = 'https://github.com/%s/%s' % (account, repodir)

            # find the owner's name
            owner = self.external.get_github_owner_name('%s/%s' % (account, repodir))

            # find the project description and license SPDX
            github_repo_data = self.external.get_github_repo_data('%s/%s' % (account, repodir))
            description = github_repo_data['description']
            if github_repo_data['license'] and github_repo_data['license']['key'] != 'other':
                license_spdx = github_repo_data['license']['spdx_id']
                license_spdx_source = LICENSE_SPDX_SOURCE_GITHUB

        elif host == 'google.golang.org':
            project_url = repo_url = 'https://%s/%s' % (host, repodir)
            owner = u'Google'
            license_spdx = 'Apache-2.0'
            license_spdx_source = LICENSE_SPDX_SOURCE_PROJECT

            # see https://cloud.google.com/go/google.golang.org
            description = {'api': u'A set of auto-generated packages that provide low-level access to various Google APIs',
                           'appengine': u'A set of packages that provide access to the Google App Engine APIs.',
                           'cloud': u'A set of idiomatically-designed packages that provide access to Google Cloud Platform APIs, including Datastore, Storage, Pub/Sub, Bigtable, BigQuery and Logging.',
                           'genproto': u'Protocol code related to Google services',
                           'grpc': u'Package grpc implements an RPC system called gRPC.'}[repodir]

        elif host == 'go.uber.org':
            # note: some URLs do not follow this pattern. See https://go.uber.org/ for list.
            project_url = repo_url = 'https://github.com/uber-go/%s' % (repodir,)
            github_account = 'uber-go'
            owner = u'Uber Technologies, Inc.'

            # these are hosted on GitHub
            github_repo_data = self.external.get_github_repo_data('uber-go/%s' % repodir)
            description = github_repo_data['description']

            if github_repo_data['license']['key'] != 'other':
                license_spdx = github_repo_data['license']['spdx_id']
                license_spdx_source = LICENSE_SPDX_SOURCE_GITHUB

        elif host == 'golang.org':
            # get rid of the 'x/' prefix
            account = None
            github_account = 'golang'
            project_url = repo_url = 'https://github.com/golang/%s' % (repodir,)
            owner = u'The Go Authors'

            github_repo_data = self.external.get_github_repo_data('golang/%s' % repodir)
            description = github_repo_data['description']

            # note: the license doesn't seem to be recognized properly by Github
            license_text_url = 'https://raw.githubusercontent.com/golang/%s/master/LICENSE' % repodir
            license_text_source = LICENSE_TEXT_SOURCE_PROJECT

        elif host == 'willnorris.com':
            project_url = repo_url = 'https://github.com/willnorris/%s' % (repodir,)
            github_account = 'willnorris'
            owner = u'Will Norris'

            # these are hosted on GitHub
            github_repo_data = self.external.get_github_repo_data('willnorris/%s' % repodir)
            description = github_repo_data['description']

            if github_repo_data['license']['key'] != 'other':
                license_spdx = github_repo_data['license']['spdx_id']
                license_spdx_source = LICENSE_SPDX_SOURCE_GITHUB

        else:
            raise Exception('unhandled host %r' % host)

        # check for inline license text, which overrides all of the above
        for lic_filename in LICENSE_FILENAMES:
            lic_full_path = os.path.join(full_path, lic_filename)
            if os.path.exists(lic_full_path):
                license_text = open(lic_full_path, 'rb').read().decode('utf-8')
                license_text_source = LICENSE_TEXT_SOURCE_INLINE
                break

        # otherwise check for URL-based license text
        if (not license_text) and license_text_url:
            license_text = self.external.slurp_url(license_text_url)

        # otherwise check for GitHub-based license
        if (not license_text) and '//github.com/' in repo_url:
            # try to use inline license text rather than falling back to SPDX
            if not license_text:
                text = self.external.search_github_for_license_file(github_account, repodir)
                if text:
                    license_text = text
                    license_text_source = LICENSE_TEXT_SOURCE_INLINE # (github)

        # check for local notice
        notice_text = None
        for not_filename in NOTICE_FILENAMES:
            not_full_path = os.path.join(full_path, not_filename)
            if os.path.exists(not_full_path):
                notice_text = open(not_full_path, 'rb').read().decode('utf-8')
                log.debug('local notice text at %s' % not_full_path)
                break

        # otherwise check for GitHub-based NOTICE
        if (not notice_text) and '//github.com/' in repo_url:
            notice_text = self.external.search_github_for_notice_file(github_account, repodir)

        # check gopkg constraint for a non-canonical source
        # (might also want to check "branch"?)
        if gopkg_constraint and 'source' in gopkg_constraint and gopkg_constraint['source'] != repo_url:
            is_modified = True

        return DependencyInfo(source_file = source_file,
                              namespace = 'golang.vendor',
                              name = dep_name,
                              owner = owner,
                              project_url = project_url,
                              repo_url = repo_url,
                              description = description or ('This is a Go package called %s.' % dep_name),
                              license_spdx = license_spdx,
                              license_spdx_source = license_spdx_source,
                              license_text = license_text,
                              license_text_source = license_text_source,
                              notice_text = notice_text,
                              discrepancies = discrepancies,
                              is_modified = is_modified,
                              )

    def handle_golang_list_scan(self, full_path, use_gopkg_toml):
        """
        Scan for golang dependencies using "go list" looking for Imports that mention "vendor".

        Due to some delicate environment variable magic,
        THIS WILL FAIL IF THE SOURCE TREE IS NOT UNDER $GOPATH.
        """
        ret = []
        log.debug('go list at %s' % full_path)

        gopkg_constraints_by_name = {}

        old_dir = os.getcwd()
        try:
            os.chdir(full_path)

            child_env = {'PATH': os.environ['PATH'], # to find the 'go' binary
                         # if full_path contains symlinks, Python's PWD will resolve those links.
                         # don't do that.
                         'PWD': full_path,
                         # assume GOPATH is set already by the caller
                         'GOPATH': os.environ['GOPATH']
                         }

            # get list of Go code paths in this project
            code_paths = subprocess.check_output(['go', 'list', './...'], shell = False, env = child_env)

            # reject paths that contain "vendor"
            code_path_list = filter(lambda x: x and ('/vendor/' not in x), code_paths.split('\n'))

            #log.debug('detected code paths:\n' + ('\n'.join(code_path_list)))

            # now run "go list" on these paths
            golist = subprocess.Popen(['go', 'list', '-f', '{{ join .Imports "\\n" }}'] + code_path_list,
                                      shell = False, env = child_env,
                                      stdout = subprocess.PIPE, stderr = subprocess.PIPE)
            golist_stdout, golist_stderr = golist.communicate()
            log.debug(golist_stdout)
            if golist.returncode != 0:
                # handle case where there were no .go files
                if golist.returncode == 1 and golist_stderr and ('no buildable Go source files in' in golist_stderr):
                    log.warn('"go list" command found no buildable Go source files')
                    return []
                else:
                    raise Exception('"go list" command failed, return code %d, output:\n%s' % (golist.returncode, golist_stderr))

            if use_gopkg_toml:
                # per Mattermost advice - check Gopkg.toml for any unusual "source" constraints
                # that indicate a modified version of the dependency is being pulled.
                gopkg_toml = toml.load(open('Gopkg.toml'))

                if 'constraint' in gopkg_toml:
                    for constraint in gopkg_toml['constraint']:
                        gopkg_constraints_by_name[constraint['name']] = constraint

        finally:
            os.chdir(old_dir)

        external_deps = set()

        for line in golist_stdout.split('\n'):
            line = line.strip()
            if not line:
                continue

            fields = line.split('/')

            # only look at "vendor" imports
            if 'vendor' in fields:
                # only look at what happens after "vendor"
                fields = fields[fields.index('vendor')+1:]
            elif '.' in fields[0]: # domain-named import
                # XXXXXX ignore ones that belong to this or related repos
                if fields[1] == 'mattermost':
                    continue
                pass
            else:
                # not a relevant dependency
                continue

            # assume that external dependencies must begin with a domain name containing a "."
            assert '.' in fields[0]

            # only keep the first three path components, to ignore sub-libraries
            fields = fields[:3]

            # further trim some special cases that host repos at toplevel
            if fields[0] in ('google.golang.org','go.uber.org'):
                fields = fields[:2]

            line = '/'.join(fields)
            external_deps.add(line)

        external_deps = sorted(list(external_deps))
        #log.debug('\n'.join(external_deps))

        for full_path in external_deps:
            dep = self.recognize_golang_vendor_dep('go list', full_path, gopkg_constraints_by_name.get(full_path))
            if dep:
                ret.append(dep)
        return ret
