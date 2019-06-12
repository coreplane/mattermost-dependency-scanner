#!/usr/bin/env python

# Break apart a monolithic NOTICE.txt into individual files, one per
# dependency, for easier diffing.

# This assumes the general format of Mattermost NOTICE.txt files, i.e. a preamble
# delimited by "---" followed by individual dependencies delimited by "## ...".

import os, sys
import getopt

if __name__ == '__main__':
    infile = None
    outdir = None

    opts, args = getopt.gnu_getopt(sys.argv[1:], '', [])

    if len(args) != 2:
        print('''usage: %s NOTICE.txt new_notice_dir''')
        sys.exit(1)

    infile = args[0]
    outdir = args[1]

    if not os.path.exists(outdir):
        os.mkdir(outdir)

    state = 'preamble0'
    outfd = open(os.path.join(outdir, 'preamble.txt'), 'w')

    for line in open(infile, 'r').readlines():
        if state == 'preamble0':
            if line.startswith('---'):
                state = 'preamble1'
                outfd.write(line)
            else:
                outfd.write(line)
        elif state == 'preamble1':
            if line.startswith('---'):
                state = 'between'
            else:
                outfd.write(line)
        elif state == 'between' and line.startswith('## '):
            name = line[3:].strip()
            if '/' in name:
                name = name.split('/')[1]
            outfd = open(os.path.join(outdir, '%s.txt' % name), 'w')
            state = 'in'
        elif state == 'in':
            if line.startswith('---\n') or line.startswith('----\n') or line.startswith('-----\n'):
                outfd.close()
                outfd = None
                state = 'between'
            else:
                outfd.write(line)
