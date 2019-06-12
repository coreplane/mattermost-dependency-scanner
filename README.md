# Mattermost Dependency License Scanner

Author: Dan Maas, CorePlane Inc.

This is the procedure for updating NOTICE.txt to reflect the current
third-party libraries referenced from Mattermost code using a
semi-automated dependency crawler. Optionally, we can also prepare an
Excel spreadsheet listing dependencies and their licenses.

This covers the following repositories:
- [mattermost-desktop](https://github.com/mattermost/desktop)
- [mattermost-mobile](https://github.com/mattermost/mattermost-mobile)
- [mattermost-redux](https://github.com/mattermost/mattermost-redux)
- [mattermost-server](https://github.com/mattermost/mattermost-server)
- [mattermost-webapp](https://github.com/mattermost/mattermost-webapp)

### Limitations
- Familiarity with Git, Python, and shell scripting is required.
- This scanner probes only first-order dependencies.

## Prerequisites
- `git clone` the above-listed Mattermost repositories on the master branch.
- The repos should all be cloned under the same directory. Call it `$MMGIT`.
  - Note: the `mattermost-desktop` repo is named `desktop` on Github,
    but should be cloned into a directory named `mattermost-desktop`,
    to simplify the steps below.
  - **Exception:** The Golang repo (`mattermost-server`) should be cloned
    into the appropriate subdirectory under
    `${GOPATH}/src/github.com/mattermost`. This is necessary for the `go
    list` command to give the right results.
- Obtain a personal GitHub access token at
  `https://github.com/settings/tokens` and export it as the
  environment variable `$GITHUB_USER_ACCESS_TOKEN`. This token will be
  used for read-only queries on the GitHub API. It is necessary to
  avoid rate limits. (if you run the dependency crawler without an
  access token, it will very quickly hit the API rate limit for
  anonymous usage).

- Create a temporary scratch directory. We’ll call it `$SCRATCH`.
- Git clone the dependency crawler from git@github.com/coreplane/mattermost-dependency-scanner
  - **Note: All the following steps will be executed from the root of this repo. **
- A local Python interpreter. The crawler has been tested with Python 2.7.10 and 3.7.3.
  - Use “pip” to install the modules listed in requirements.txt. It is OK to use a virtual environment or a Docker-based Python if you wish.

## Procedure

1. **Split the current NOTICE.txt files into one file per dependency**, and write these into the scratch directory. This makes the diffing process easier.

```
for MODULE in server webapp desktop mobile redux; do
  python split-notices.py \
  ${MMGIT}/mattermost-${MODULE}/NOTICE.txt \
  ${SCRATCH}/notice-mattermost-${MODULE}
done
```

2. **Prepare new NOTICE.txt files** using `crawler.py`, which crawls the appropriate package libraries (NPM or Golang) for copyright license metadata.

The `crawler.py` command needs slightly different options for the Golang mattermost-server repo and the other, JavaScript-based repos.

### Golang repo:

```
MODULE=server
# remove any files from previous run
rm -rf ${SCRATCH}/notice-mattermost-${MODULE}-new*
# initialize new NOTICE.txt with the preamble
(cat ${SCRATCH}/notice-mattermost-${MODULE}/preamble.txt && echo "-----" && echo) > ${SCRATCH}/notice-mattermost-${MODULE}-new.txt
# run the dependency crawler (this will take a few minutes)
python crawler.py \
  --dir=${GOPATH}/src/github.com/mattermost/mattermost-${MODULE} \
  --full-text --use-go-list \
  --xlsx ${SCRATCH}/notice-mattermost-${MODULE}-new.xlsx \
  >> ${SCRATCH}/notice-mattermost-${MODULE}-new.txt
# run split-notices to split the new NOTICE.txt into per-license files for easier diffing
python split-notices.py \
  ${SCRATCH}/notice-mattermost-${MODULE}-new.txt \
  ${SCRATCH}/notice-mattermost-${MODULE}-new
# create a diff
diff -bruN \
  ${SCRATCH}/notice-mattermost-${MODULE} \
  ${SCRATCH}/notice-mattermost-${MODULE}-new \
  > ${SCRATCH}/notice-mattermost-${MODULE}.diff
```

### JavaScript repos:
```
for MODULE in webapp desktop mobile redux; do
  # remove any files from previous run
  rm -rf ${SCRATCH}/notice-mattermost-${MODULE}-new*
  # initialize new NOTICE.txt with the preamble
  (cat ${SCRATCH}/notice-mattermost-${MODULE}/preamble.txt && echo "-----" && echo) > ${SCRATCH}/notice-mattermost-${MODULE}-new.txt
  # run the dependency crawler (this will take a few minutes)
  python crawler.py \
  --dir=${MMGIT}/mattermost-${MODULE} \
  --full-text \
  --xlsx ${SCRATCH}/notice-mattermost-${MODULE}-new.xlsx \
  >> ${SCRATCH}/notice-mattermost-${MODULE}-new.txt
# run split-notices to split the new NOTICE.txt into per-license files for easier diffing
python split-notices.py \
  ${SCRATCH}/notice-mattermost-${MODULE}-new.txt \
  ${SCRATCH}/notice-mattermost-${MODULE}-new
# create a diff
diff -bruN \
  ${SCRATCH}/notice-mattermost-${MODULE} \
  ${SCRATCH}/notice-mattermost-${MODULE}-new \
  > ${SCRATCH}/notice-mattermost-${MODULE}.diff
done
```

At the end of this process, your `${SCRATCH}` directory will contain new NOTICE.txt files for each repo, plus diffs showing what changed relative to the current Git masters.

Note: if “crawler.py” fails with an error message, it’s usually
because of a new dependency with incomplete copyright metadata. You
may need to insert a special-case fix for this. See dependencies.py.

3. **Review the diffs manually** for any obvious errors.

**Note:** some minor “noise” is to be expected, e.g. removal of some manually-inserted notes about dual-licensed dependencies, or changes in line ending format within license files. Please edit the diffs to remove unimportant changes like this. A diff editor like SourceTree makes it fast and easy.

**For each repo that has significant diffs...**

4. Make a Git branch called `notice-update-YYYYMMDD` (with the current
date) and copy in the new NOTICE.txt:
```
DATE=`date +%Y%m%d`
for MODULE in server webapp desktop mobile redux; do
 (cd ${MMGIT}/mattermost-${MODULE} && \
 git checkout -b notice-update-${DATE} && \
 cp ${SCRATCH}/notice-mattermost-${MODULE}-new.txt NOTICE.txt)
done
```

5. **Git commit, push to GitHub, and submit a pull request.**

## (Optional) Excel output

As a by-product of the above steps, you will also find Excel-format .xlsx files listing the first-order dependencies of each Mattermost repo. These can be assembled by hand into a master .xlsx as need for legal compliance.
