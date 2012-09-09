#!/usr/bin/env python
# Copyright (c) 2011 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Get rietveld stats about the review you done, or forgot to do.

Example:
  - my_reviews.py -r me@chromium.org -Q  for stats for last quarter.
"""
import datetime
import math
import optparse
import os
import sys

import rietveld


def username(email):
  """Keeps the username of an email address."""
  return email.split('@', 1)[0]


def to_datetime(string):
  """Load UTC time as a string into a datetime object."""
  try:
    # Format is 2011-07-05 01:26:12.084316
    return datetime.datetime.strptime(
        string.split('.', 1)[0], '%Y-%m-%d %H:%M:%S')
  except ValueError:
    return datetime.datetime.strptime(string, '%Y-%m-%d')


def to_time(seconds):
  """Convert a number of seconds into human readable compact string."""
  prefix = ''
  if seconds < 0:
    prefix = '-'
    seconds *= -1
  minutes = math.floor(seconds / 60)
  seconds -= minutes * 60
  hours = math.floor(minutes / 60)
  minutes -= hours * 60
  days = math.floor(hours / 24)
  hours -= days * 24
  out = []
  if days > 0:
    out.append('%dd' % days)
  if hours > 0 or days > 0:
    out.append('%02dh' % hours)
  if minutes > 0 or hours > 0 or days > 0:
    out.append('%02dm' % minutes)
  if seconds > 0 and not out:
    # Skip seconds unless there's only seconds.
    out.append('%02ds' % seconds)
  return prefix + ''.join(out)


class Stats(object):
  def __init__(self):
    self.total = 0
    self.actually_reviewed = 0
    self.average_latency = 0.
    self.number_latency = 0
    self.lgtms = 0
    self.multiple_lgtms = 0
    self.drive_by = 0
    self.not_requested = 0
    self.self_review = 0

    self.percent_done = 0.
    self.percent_lgtm = 0.
    self.percent_drive_by = 0.
    self.percent_not_requested = 0.
    self.days = 0
    self.review_per_day = 0.
    self.review_done_per_day = 0.

  def add_latency(self, latency):
    self.average_latency = (
        (self.average_latency * self.number_latency + latency) /
        (self.number_latency + 1.))
    self.number_latency += 1

  def finalize(self, first_day, last_day):
    if self.total:
      self.percent_done = (self.actually_reviewed * 100. / self.total)
    if self.actually_reviewed:
      self.percent_lgtm = (self.lgtms * 100. / self.actually_reviewed)
      self.percent_drive_by = (self.drive_by * 100. / self.actually_reviewed)
      self.percent_not_requested = (
          self.not_requested * 100. / self.actually_reviewed)
    if first_day and last_day:
      self.days = (to_datetime(last_day) - to_datetime(first_day)).days + 1
    if self.days:
      self.review_per_day = self.total * 1. / self.days
      self.review_done_per_day = self.actually_reviewed * 1. / self.days


def _process_issue_lgtms(issue, reviewer, stats):
  """Calculates LGTMs stats."""
  stats.actually_reviewed += 1
  reviewer_lgtms = len([
    msg for msg in issue['messages']
    if msg['approval'] and msg['sender'] == reviewer])
  if reviewer_lgtms > 1:
    stats.multiple_lgtms += 1
    return ' X '
  if reviewer_lgtms:
    stats.lgtms += 1
    return ' x '
  else:
    return ' o '


def _process_issue_latency(issue, reviewer, stats):
  """Calculates latency for an issue that was actually reviewed."""
  from_owner = [
    msg for msg in issue['messages'] if msg['sender'] == issue['owner_email']
  ]
  if not from_owner:
    # Probably requested by email.
    stats.not_requested += 1
    return '<no rqst sent>'

  first_msg_from_owner = None
  latency = None
  received = False
  for index, msg in enumerate(issue['messages']):
    if not first_msg_from_owner and msg['sender'] == issue['owner_email']:
      first_msg_from_owner = msg
    if index and not received and msg['sender'] == reviewer:
      # Not first email, reviewer never received one, reviewer sent a mesage.
      stats.drive_by += 1
      return '<drive-by>'
    received |= reviewer in msg['recipients']

    if first_msg_from_owner and msg['sender'] == reviewer:
      delta = msg['date'] - first_msg_from_owner['date']
      latency = delta.seconds + delta.days * 24 * 3600
      break

  if latency is None:
    stats.not_requested += 1
    return '<no rqst sent>'
  if latency > 0:
    stats.add_latency(latency)
  else:
    stats.not_requested += 1
  return to_time(latency)


def _process_issue(issue):
  """Preprocesses the issue to simplify the remaining code."""
  issue['owner_email'] = username(issue['owner_email'])
  issue['reviewers'] = set(username(r) for r in issue['reviewers'])
  # By default, hide commit-bot.
  issue['reviewers'] -= set(['commit-bot'])
  for msg in issue['messages']:
    msg['sender'] = username(msg['sender'])
    msg['recipients'] = [username(r) for r in msg['recipients']]
    # Convert all times to datetime instances.
    msg['date'] = to_datetime(msg['date'])
  issue['messages'].sort(key=lambda x: x['date'])


def print_issue(issue, reviewer, stats):
  """Process an issue and prints stats about it."""
  stats.total += 1
  _process_issue(issue)
  if issue['owner_email'] == reviewer:
    stats.self_review += 1
    latency = '<self review>'
    reviewed = ''
  elif any(msg['sender'] == reviewer for msg in issue['messages']):
    reviewed = _process_issue_lgtms(issue, reviewer, stats)
    latency = _process_issue_latency(issue, reviewer, stats)
  else:
    latency = 'N/A'
    reviewed = ''

  # More information is available, print issue.keys() to see them.
  print '%7d %10s %3s %14s %-15s  %s' % (
      issue['issue'],
      issue['created'][:10],
      reviewed,
      latency,
      issue['owner_email'],
      ', '.join(sorted(issue['reviewers'])))


def print_reviews(reviewer, created_after, created_before, instance_url):
  """Prints issues |reviewer| received and potentially reviewed."""
  remote = rietveld.Rietveld(instance_url, None, None)

  # The stats we gather. Feel free to send me a CL to get more stats.
  stats = Stats()

  last_issue = None
  first_day = None
  last_day = None

  # Column sizes need to match print_issue() output.
  print >> sys.stderr, (
      'Issue   Creation   Did         Latency Owner           Reviewers')

  # See def search() in rietveld.py to see all the filters you can use.
  for issue in remote.search(
      reviewer=reviewer,
      created_after=created_after,
      created_before=created_before,
      with_messages=True):
    last_issue = issue
    if not first_day:
      first_day = issue['created'][:10]
    print_issue(issue, username(reviewer), stats)
  if last_issue:
    last_day = last_issue['created'][:10]
  stats.finalize(first_day, last_day)

  print >> sys.stderr, (
      '%s reviewed %d issues out of %d (%1.1f%%). %d were self-review.' %
      (reviewer, stats.actually_reviewed, stats.total, stats.percent_done,
        stats.self_review))
  print >> sys.stderr, (
      '%4.1f review request/day during %3d days   (%4.1f r/d done).' % (
      stats.review_per_day, stats.days, stats.review_done_per_day))
  print >> sys.stderr, (
      '%4d were drive-bys                       (%5.1f%% of reviews done).' % (
        stats.drive_by, stats.percent_drive_by))
  print >> sys.stderr, (
      '%4d were requested over IM or irc        (%5.1f%% of reviews done).' % (
        stats.not_requested, stats.percent_not_requested))
  print >> sys.stderr, (
      ('%4d issues LGTM\'d                        (%5.1f%% of reviews done),'
       ' gave multiple LGTMs on %d issues.') % (
      stats.lgtms, stats.percent_lgtm, stats.multiple_lgtms))
  print >> sys.stderr, (
      'Average latency from request to first comment is %s.' %
      to_time(stats.average_latency))


def print_count(reviewer, created_after, created_before, instance_url):
  remote = rietveld.Rietveld(instance_url, None, None)
  print len(list(remote.search(
      reviewer=reviewer,
      created_after=created_after,
      created_before=created_before,
      keys_only=True)))


def get_previous_quarter(today):
  """There are four quarters, 01-03, 04-06, 07-09, 10-12.

  If today is in the last month of a quarter, assume it's the current quarter
  that is requested.
  """
  end_year = today.year
  end_month = today.month - (today.month % 3) + 1
  if end_month <= 0:
    end_year -= 1
    end_month += 12
  if end_month > 12:
    end_year += 1
    end_month -= 12
  end = '%d-%02d-01' % (end_year, end_month)
  begin_year = end_year
  begin_month = end_month - 3
  if begin_month <= 0:
    begin_year -= 1
    begin_month += 12
  begin = '%d-%02d-01' % (begin_year, begin_month)
  return begin, end


def main():
  # Silence upload.py.
  rietveld.upload.verbosity = 0
  today = datetime.date.today()
  begin, end = get_previous_quarter(today)
  parser = optparse.OptionParser(description=sys.modules[__name__].__doc__)
  parser.add_option(
      '--count', action='store_true',
      help='Just count instead of printing individual issues')
  parser.add_option(
      '-r', '--reviewer', metavar='<email>',
      default=os.environ.get('EMAIL_ADDRESS'),
      help='Filter on issue reviewer, default=%default')
  parser.add_option(
      '-b', '--begin', metavar='<date>',
      help='Filter issues created after the date')
  parser.add_option(
      '-e', '--end', metavar='<date>',
      help='Filter issues created before the date')
  parser.add_option(
      '-Q', '--last_quarter', action='store_true',
      help='Use last quarter\'s dates, e.g. %s to %s' % (
        begin, end))
  parser.add_option(
      '-i', '--instance_url', metavar='<host>',
      default='http://codereview.chromium.org',
      help='Host to use, default is %default')
  # Remove description formatting
  parser.format_description = (
      lambda _: parser.description)  # pylint: disable=E1101
  options, args = parser.parse_args()
  if args:
    parser.error('Args unsupported')
  if not options.reviewer:
    parser.error('$EMAIL_ADDRESS is not set, please use -r')
  print >> sys.stderr, 'Searching for reviews by %s' % options.reviewer
  if options.last_quarter:
    options.begin = begin
    options.end = end
    print >> sys.stderr, 'Using range %s to %s' % (
        options.begin, options.end)
  if options.count:
    print_count(
        options.reviewer,
        options.begin,
        options.end,
        options.instance_url)
  else:
    print_reviews(
        options.reviewer,
        options.begin,
        options.end,
        options.instance_url)
  return 0


if __name__ == '__main__':
  sys.exit(main())