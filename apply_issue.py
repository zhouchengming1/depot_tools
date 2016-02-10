#!/usr/bin/env python
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Applies an issue from Rietveld.
"""
import getpass
import json
import logging
import optparse
import os
import subprocess
import sys
import urllib2


import annotated_gclient
import auth
import checkout
import fix_encoding
import gclient_utils
import rietveld
import scm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


RETURN_CODE_OK               = 0
RETURN_CODE_OTHER_FAILURE    = 1  # any other failure, likely patch apply one.
RETURN_CODE_ARGPARSE_FAILURE = 2  # default in python.
RETURN_CODE_INFRA_FAILURE    = 3  # considered as infra failure.


class Unbuffered(object):
  """Disable buffering on a file object."""
  def __init__(self, stream):
    self.stream = stream

  def write(self, data):
    self.stream.write(data)
    self.stream.flush()

  def __getattr__(self, attr):
    return getattr(self.stream, attr)


def _get_arg_parser():
  parser = optparse.OptionParser(description=sys.modules[__name__].__doc__)
  parser.add_option(
      '-v', '--verbose', action='count', default=0,
      help='Prints debugging infos')
  parser.add_option(
      '-e', '--email',
      help='Email address to access rietveld.  If not specified, anonymous '
           'access will be used.')
  parser.add_option(
      '-E', '--email-file',
      help='File containing the email address to access rietveld. '
           'If not specified, anonymous access will be used.')
  parser.add_option(
      '-k', '--private-key-file',
      help='Path to file containing a private key in p12 format for OAuth2 '
           'authentication with "notasecret" password (as generated by Google '
           'Cloud Console).')
  parser.add_option(
      '-i', '--issue', type='int', help='Rietveld issue number')
  parser.add_option(
      '-p', '--patchset', type='int', help='Rietveld issue\'s patchset number')
  parser.add_option(
      '-r',
      '--root_dir',
      default=os.getcwd(),
      help='Root directory to apply the patch')
  parser.add_option(
      '-s',
      '--server',
      default='http://codereview.chromium.org',
      help='Rietveld server')
  parser.add_option('--no-auth', action='store_true',
                    help='Do not attempt authenticated requests.')
  parser.add_option('--revision-mapping', default='{}',
                    help='When running gclient, annotate the got_revisions '
                         'using the revision-mapping.')
  parser.add_option('-f', '--force', action='store_true',
                    help='Really run apply_issue, even if .update.flag '
                         'is detected.')
  parser.add_option('-b', '--base_ref', help='DEPRECATED do not use.')
  parser.add_option('--whitelist', action='append', default=[],
                    help='Patch only specified file(s).')
  parser.add_option('--blacklist', action='append', default=[],
                    help='Don\'t patch specified file(s).')
  parser.add_option('-d', '--ignore_deps', action='store_true',
                    help='Don\'t run gclient sync on DEPS changes.')

  auth.add_auth_options(parser)
  return parser


def main():
  # TODO(pgervais,tandrii): split this func, it's still too long.
  sys.stdout = Unbuffered(sys.stdout)
  parser = _get_arg_parser()
  options, args = parser.parse_args()
  auth_config = auth.extract_auth_config_from_options(options)

  if options.whitelist and options.blacklist:
    parser.error('Cannot specify both --whitelist and --blacklist')

  if options.email and options.email_file:
    parser.error('-e and -E options are incompatible')

  if (os.path.isfile(os.path.join(os.getcwd(), 'update.flag'))
      and not options.force):
    print 'update.flag file found: bot_update has run and checkout is already '
    print 'in a consistent state. No actions will be performed in this step.'
    return 0

  logging.basicConfig(
      format='%(levelname)5s %(module)11s(%(lineno)4d): %(message)s',
      level=[logging.WARNING, logging.INFO, logging.DEBUG][
          min(2, options.verbose)])
  if args:
    parser.error('Extra argument(s) "%s" not understood' % ' '.join(args))
  if not options.issue:
    parser.error('Require --issue')
  options.server = options.server.rstrip('/')
  if not options.server:
    parser.error('Require a valid server')

  options.revision_mapping = json.loads(options.revision_mapping)

  # read email if needed
  if options.email_file:
    if not os.path.exists(options.email_file):
      parser.error('file does not exist: %s' % options.email_file)
    with open(options.email_file, 'rb') as f:
      options.email = f.read().strip()

  print('Connecting to %s' % options.server)
  # Always try un-authenticated first, except for OAuth2
  if options.private_key_file:
    # OAuth2 authentication
    rietveld_obj = rietveld.JwtOAuth2Rietveld(options.server,
                                     options.email,
                                     options.private_key_file)
    try:
      properties = rietveld_obj.get_issue_properties(options.issue, False)
    except urllib2.URLError:
      logging.exception('failed to fetch issue properties')
      sys.exit(RETURN_CODE_INFRA_FAILURE)
  else:
    # Passing None as auth_config disables authentication.
    rietveld_obj = rietveld.Rietveld(options.server, None)
    properties = None
    # Bad except clauses order (HTTPError is an ancestor class of
    # ClientLoginError)
    # pylint: disable=E0701
    try:
      properties = rietveld_obj.get_issue_properties(options.issue, False)
    except urllib2.HTTPError as e:
      if e.getcode() != 302:
        raise
      if options.no_auth:
        exit('FAIL: Login detected -- is issue private?')
      # TODO(maruel): A few 'Invalid username or password.' are printed first,
      # we should get rid of those.
    except urllib2.URLError:
      logging.exception('failed to fetch issue properties')
      return RETURN_CODE_INFRA_FAILURE
    except rietveld.upload.ClientLoginError as e:
      # Fine, we'll do proper authentication.
      pass
    if properties is None:
      rietveld_obj = rietveld.Rietveld(options.server, auth_config,
                                       options.email)
      try:
        properties = rietveld_obj.get_issue_properties(options.issue, False)
      except rietveld.upload.ClientLoginError as e:
        print('Accessing the issue requires proper credentials.')
        return RETURN_CODE_OTHER_FAILURE
      except urllib2.URLError:
        logging.exception('failed to fetch issue properties')
        return RETURN_CODE_INFRA_FAILURE

  if not options.patchset:
    options.patchset = properties['patchsets'][-1]
    print('No patchset specified. Using patchset %d' % options.patchset)

  issues_patchsets_to_apply = [(options.issue, options.patchset)]
  try:
    depends_on_info = rietveld_obj.get_depends_on_patchset(
        options.issue, options.patchset)
  except urllib2.URLError:
    logging.exception('failed to fetch depends_on_patchset')
    return RETURN_CODE_INFRA_FAILURE

  while depends_on_info:
    depends_on_issue = int(depends_on_info['issue'])
    depends_on_patchset = int(depends_on_info['patchset'])
    try:
      depends_on_info = rietveld_obj.get_depends_on_patchset(depends_on_issue,
                                                    depends_on_patchset)
      issues_patchsets_to_apply.insert(0, (depends_on_issue,
                                           depends_on_patchset))
    except urllib2.HTTPError:
      print ('The patchset that was marked as a dependency no longer '
             'exists: %s/%d/#ps%d' % (
                 options.server, depends_on_issue, depends_on_patchset))
      print 'Therefore it is likely that this patch will not apply cleanly.'
      print
      depends_on_info = None
    except urllib2.URLError:
      logging.exception('failed to fetch dependency issue')
      return RETURN_CODE_INFRA_FAILURE

  num_issues_patchsets_to_apply = len(issues_patchsets_to_apply)
  if num_issues_patchsets_to_apply > 1:
    print
    print 'apply_issue.py found %d dependent CLs.' % (
        num_issues_patchsets_to_apply - 1)
    print 'They will be applied in the following order:'
    num = 1
    for issue_to_apply, patchset_to_apply in issues_patchsets_to_apply:
      print '  #%d %s/%d/#ps%d' % (
          num, options.server, issue_to_apply, patchset_to_apply)
      num += 1
    print

  for issue_to_apply, patchset_to_apply in issues_patchsets_to_apply:
    issue_url = '%s/%d/#ps%d' % (options.server, issue_to_apply,
                                 patchset_to_apply)
    print('Downloading patch from %s' % issue_url)
    try:
      patchset = rietveld_obj.get_patch(issue_to_apply, patchset_to_apply)
    except urllib2.HTTPError:
      print(
          'Failed to fetch the patch for issue %d, patchset %d.\n'
          'Try visiting %s/%d') % (
              issue_to_apply, patchset_to_apply,
              options.server, issue_to_apply)
      # If we got this far, then this is likely missing patchset.
      # Thus, it's not infra failure.
      return RETURN_CODE_OTHER_FAILURE
    except urllib2.URLError:
      logging.exception(
          'Failed to fetch the patch for issue %d, patchset %d',
          issue_to_apply, patchset_to_apply)
      return RETURN_CODE_INFRA_FAILURE
    if options.whitelist:
      patchset.patches = [patch for patch in patchset.patches
                          if patch.filename in options.whitelist]
    if options.blacklist:
      patchset.patches = [patch for patch in patchset.patches
                          if patch.filename not in options.blacklist]
    for patch in patchset.patches:
      print(patch)
    full_dir = os.path.abspath(options.root_dir)
    scm_type = scm.determine_scm(full_dir)
    if scm_type == 'svn':
      scm_obj = checkout.SvnCheckout(full_dir, None, None, None, None)
    elif scm_type == 'git':
      scm_obj = checkout.GitCheckout(full_dir, None, None, None, None)
    elif scm_type == None:
      scm_obj = checkout.RawCheckout(full_dir, None, None)
    else:
      parser.error('Couldn\'t determine the scm')

    # TODO(maruel): HACK, remove me.
    # When run a build slave, make sure buildbot knows that the checkout was
    # modified.
    if options.root_dir == 'src' and getpass.getuser() == 'chrome-bot':
      # See sourcedirIsPatched() in:
      # http://src.chromium.org/viewvc/chrome/trunk/tools/build/scripts/slave/
      #    chromium_commands.py?view=markup
      open('.buildbot-patched', 'w').close()

    print('\nApplying the patch from %s' % issue_url)
    try:
      scm_obj.apply_patch(patchset, verbose=True)
    except checkout.PatchApplicationFailed as e:
      print(str(e))
      print('CWD=%s' % os.getcwd())
      print('Checkout path=%s' % scm_obj.project_path)
      return RETURN_CODE_OTHER_FAILURE

  if ('DEPS' in map(os.path.basename, patchset.filenames)
      and not options.ignore_deps):
    gclient_root = gclient_utils.FindGclientRoot(full_dir)
    if gclient_root and scm_type:
      print(
          'A DEPS file was updated inside a gclient checkout, running gclient '
          'sync.')
      gclient_path = os.path.join(BASE_DIR, 'gclient')
      if sys.platform == 'win32':
        gclient_path += '.bat'
      with annotated_gclient.temp_filename(suffix='gclient') as f:
        cmd = [
            gclient_path, 'sync',
            '--nohooks',
            '--delete_unversioned_trees',
            ]
        if scm_type == 'svn':
          cmd.extend(['--revision', 'BASE'])
        if options.revision_mapping:
          cmd.extend(['--output-json', f])

        retcode = subprocess.call(cmd, cwd=gclient_root)

        if retcode == 0 and options.revision_mapping:
          revisions = annotated_gclient.parse_got_revision(
              f, options.revision_mapping)
          annotated_gclient.emit_buildprops(revisions)

        return retcode
  return RETURN_CODE_OK


if __name__ == "__main__":
  fix_encoding.fix_encoding()
  try:
    sys.exit(main())
  except KeyboardInterrupt:
    sys.stderr.write('interrupted\n')
    sys.exit(RETURN_CODE_OTHER_FAILURE)
