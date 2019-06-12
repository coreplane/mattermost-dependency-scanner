#!/usr/bin/env python

# Dependency crawler driver script

from __future__ import print_function, absolute_import
import sys
import os.path
import getopt
import logging
import xlsxwriter
import time

from Dependencies import DependenciesModule, DependenciesModuleConfig, DISCREP_NONSTANDARD_LICENSE

# an access token for a user account, not associated with any app, that will be
# used by the dependency module when querying dependency owner/repo data from Github.
# This is not for access to private resources. It's just to avoid rate limiting.
github_user_access_token = os.getenv('GITHUB_USER_ACCESS_TOKEN')

if not github_user_access_token:
    raise Exception('$GITHUB_USER_ACCESS_TOKEN required. Generate one from https://github.com/settings/tokens and export as this environment variable.')

logging.basicConfig()

def run_dependencies_one(start_path, golang_scan_method, use_gopkg_toml, include_full_text):
    mod = DependenciesModule(DependenciesModuleConfig(start_path = start_path,
                                                      github_access_token = github_user_access_token,
                                                      mock_requests = False,
                                                      golang_scan_method = golang_scan_method,
                                                      use_gopkg_toml = use_gopkg_toml,
                                                      ))
    return mod.get_dependency_info()

def run_dependencies(start_path_list, golang_scan_method, use_gopkg_toml,
                     check_quality, include_full_text,
                     output_xlsx_filename,
                     report_discrepancies_fd, report_discrepancies_xlsx_filename):

    # map from source_project name -> list_of_deps
    dep_info_by_proj = {}

    # To de-dupe deps found in multiple projects:
    # map from (namespace, name) -> (dep, source_project_where_first_found)
    dep_info_by_key = {}

    for start_path in start_path_list:
        source_project = os.path.basename(start_path)
        deps = run_dependencies_one(start_path, golang_scan_method, use_gopkg_toml, include_full_text)
        assert source_project not in dep_info_by_proj
        dep_info_by_proj[source_project] = deps
        for dep in deps:
            key = (dep.namespace, dep.name)
            if key not in dep_info_by_key:
                dep_info_by_key[key] = (dep, source_project)

    # list of de-duped (dep, source_project_where_first_found)
    dep_proj_tuples = [v for k,v in sorted(dep_info_by_key.items())]

    # dep list, de-duped across projects
    dep_info_list = [x[0] for x in dep_proj_tuples]

    if report_discrepancies_fd:
        print_dependencies_discrepancies(report_discrepancies_fd, dep_info_list)
        if report_discrepancies_xlsx_filename:
            write_dependencies_discrepancies_to_xlsx(dep_proj_tuples, report_discrepancies_xlsx_filename)

    print_dependencies_notice(dep_info_list, include_full_text)
    if output_xlsx_filename:
        write_dependencies_to_xlsx(dep_info_by_proj, output_xlsx_filename)

    if check_quality:
        print_dependencies_quality(dep_info_list)

def print_dependencies_notice(dep_info_list, include_full_text):
    output_list = []
    for dep in dep_info_list:
        text = dep.to_markdown(include_license_spdx = True, # not include_full_text,
                               include_license_url = not include_full_text)
        if include_full_text:
            text += u'\n\n' + dep.to_body_text()

        if dep.notice_text and ('Mattermost, Inc' not in dep.notice_text):
            text += u'\n\n* This package includes the following NOTICE:\n\n'
            text += dep.notice_text.rstrip()

        output_list.append(text)

    print(u'\n\n---\n\n'.join(output_list).encode('utf-8'))

def write_dependencies_to_xlsx(dep_info_by_proj, output_xlsx_filename):
    """ Create an Excel spreadsheet file listing all dependencies in all projects. """
    workbook = xlsxwriter.Workbook(output_xlsx_filename)
    worksheet = workbook.add_worksheet()
    # header row
    worksheet.write('A1', 'Name of Open Source Software')
    worksheet.write('B1', 'Link to Software License')
    worksheet.write('C1', 'License Type (SPDX ID)')
    worksheet.write('D1', 'Where Used')
    worksheet.write('E1', 'Functionality')
    # per-project, per-dep rows
    last_row = 1
    for source_project, dep_info_list in dep_info_by_proj.items():
        for i, dep in enumerate(dep_info_list):
            # name
            worksheet.write('A%d' % (i+2), dep.name)
            # URL to software license
            worksheet.write('B%d' % (i+2), dep.get_license_text_url(compound_ok = True))
            # license type
            worksheet.write('C%d' % (i+2), dep.license_spdx)
            # where used
            where_used = '%s (%s dependency)' % (source_project, dep.namespace)
            worksheet.write('D%d' % (i+2), where_used)
            worksheet.write('E%d' % (i+2), dep.description)
            last_row = max(last_row, i+2)
    # footer row
    last_row += 1
    time_string = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
    worksheet.write('A%d' % last_row, 'Generated by mattermost-dependency-scanner at %s' % time_string)
    workbook.close()

def print_dependencies_quality(dep_info_list):
    """ Print a report on the quality of data gathered for this list of deps. """
    for FIELD in ('source_file', 'owner', 'project_url', 'repo_url', 'description',):
        print('--- %s ---' % FIELD)
        for dep in dep_info_list:
            print(u'%-30s %r' % (dep.name, getattr(dep, FIELD, 'None')))

    # show license data separately
    print('--- license ---')
    for dep in dep_info_list:
        if not dep.license_text:
            text = u'*** MISSING ***'
        else:
            text = dep.license_text[:40] + '...'
        print(u'%-30s %-26s %-20s %-20s %46r %r' % (dep.name, dep.license_spdx_source, dep.license_spdx,
                                                   dep.license_text_source, text, dep.notice_text))

def print_dependencies_discrepancies(output_fd, dep_info_list):
    """ Print a report of problems noted in the upstreams for this list of deps. """
    by_discrep_type = {}
    for dep in dep_info_list:
        for discrep in dep.discrepancies:
            # these aren't worth reporting
            if discrep in (DISCREP_NONSTANDARD_LICENSE,):
                continue
            if discrep not in by_discrep_type:
                by_discrep_type[discrep] = []
            by_discrep_type[discrep].append((dep.namespace, dep.name))
    if by_discrep_type:
        for k in sorted(by_discrep_type.keys()):
            output_fd.write('--- %s ---\n' % k)
            output_fd.write('\n'.join(map(lambda namespace_name: '%s/%s' % namespace_name, sorted(by_discrep_type[k]))))
            output_fd.write('\n')
    else:
        output_fd.write('No discrepancies.\n')

def write_dependencies_discrepancies_to_xlsx(dep_proj_tuples, output_xlsx_filename):
    workbook = xlsxwriter.Workbook(output_xlsx_filename)
    worksheet = workbook.add_worksheet()

    # header row
    worksheet.write('A1', 'Source Project')
    worksheet.write('B1', 'Namespace')
    worksheet.write('C1', 'Name')
    worksheet.write('D1', 'Discrepancy')
    worksheet.write('E1', 'Repo URL')

    # per-dep rows
    next_row = 2

    for dep_proj in dep_proj_tuples:
        dep, source_project = dep_proj

        discrep_list = filter(lambda d: d not in (DISCREP_NONSTANDARD_LICENSE,), dep.discrepancies)

        for discrep in discrep_list:
            # source_project
            worksheet.write('A%d' % next_row, source_project)
            # namespace
            worksheet.write('B%d' % next_row, dep.namespace)
            # name
            worksheet.write('C%d' % next_row, dep.name)
            # discrepancy
            worksheet.write('D%d' % next_row, discrep)
            # repo URL
            if dep.repo_url:
                worksheet.write('E%d' % next_row, dep.repo_url)
            next_row += 1

    # footer row
    time_string = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
    worksheet.write('A%d' % next_row, 'Generated by mattermost-dependency-scanner at %s' % time_string)
    workbook.close()

if __name__ == '__main__':
    indir_list = []

    # dependency options
    check_dep_quality = False
    include_full_text = False
    output_xlsx_filename = None
    report_discrepancies_fd = None
    report_discrepancies_xlsx_filename = None
    golang_scan_method = DependenciesModuleConfig.GOLANG_SCAN_VENDOR_DIR
    use_gopkg_toml = False

    opts, args = getopt.gnu_getopt(sys.argv[1:], '', ['dir=','qa','full-text',
                                                      'xlsx=',
                                                      'discrepancies=','discrepancies-xlsx=',
                                                      'use-go-list','use-gopkg-toml'])

    logging.getLogger('dependencies').setLevel(os.getenv('LOGLEVEL') or 'DEBUG')

    for key, val in opts:
        if key == '--dir': indir_list.append(val)
        elif key == '--qa': check_dep_quality = True
        elif key == '--full-text': include_full_text = True
        elif key == '--xlsx': output_xlsx_filename = val
        elif key == '--discrepancies': report_discrepancies_fd = open(val, 'w') if val != '-' else sys.stdout
        elif key == '--discrepancies-xlsx': report_discrepancies_xlsx_filename = val
        elif key == '--use-go-list': golang_scan_method = DependenciesModuleConfig.GOLANG_SCAN_GO_LIST_VENDOR
        elif key == '--use-gopkg-toml': use_gopkg_toml = True

    if not indir_list:
        print('''usage: %s --dir=... [options]

Options:
  --dir=START-DIR     search this directory for dependencies (can be used multiple times)
  --qa                print report of depedency metadata quality
  --discrepancies     print report of specific dependency metadata problems
  --discrepancies-xlsx write report of specific dependency metadata problems in XLSX format
  --use-go-list       use "go list" to determine Golang dependencies (without this option, Golang dependencies are found by crawling vendor/)
  --use-gopkg-toml    read gopkg.toml to determine Golang dependencies (ALPHA, not fully supported)
''')
        sys.exit(1)

    run_dependencies(indir_list, golang_scan_method, use_gopkg_toml,
                     check_dep_quality, include_full_text,
                     output_xlsx_filename,
                     report_discrepancies_fd, report_discrepancies_xlsx_filename)
