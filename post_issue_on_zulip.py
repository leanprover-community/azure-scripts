#!/usr/bin/env python3

import sys
import zulip
import github
import datetime
import random
import re
import time

zulip_token = sys.argv[1]
gh_token = sys.argv[2]

zulip_client = zulip.Client(email="random-issue-bot@zulipchat.com", api_key=zulip_token, site="https://leanprover.zulipchat.com")

def message_date(id):
    history = zulip_client.get_message_history(id)
    print(history)
    # We're limited to 200 API calls per minute, this should give us a decent amount of leeway
    # https://zulip.com/api/rest-error-handling#rate-limit-exceeded
    time.sleep(0.4)
    return history['message_history'][0]['timestamp']

posted_topics = zulip_client.get_stream_topics(zulip_client.get_stream_id('triage')['stream_id'])['topics']
pattern = re.compile(r'!4#(\d+)')
posted_topics = {int(v[0]): message_date(t['max_id']) for (t, v) in ((t, pattern.findall(t['name'])) for t in posted_topics) if len(v) > 0}

print(posted_topics)

gh = github.Github(login_or_token=gh_token)
mathlib = gh.get_repo('leanprover-community/mathlib4')

open_items = mathlib.get_issues(state='open')
open_prs = []
open_issues = []

print(f'Found {open_items.totalCount} open item(s) (PRs and issues).')

for i in open_items:
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    delta = datetime.timedelta(days=7)
    min_age = now - delta
    if i.updated_at < min_age \
         and 'blocked-by-other-PR' not in [l.name for l in i.labels] \
             and not (i.number in posted_topics and datetime.datetime.fromtimestamp(posted_topics[i.number], tz=datetime.timezone.utc) > min_age):
        if i.pull_request:
            open_prs.append(i)
        else:
            open_issues.append(i)

print(f'Found {len(open_issues)} open issue(s) after filtering.')
print(f'Found {len(open_prs)} open PR(s) after filtering.')

def post_random(select_from, kind):
    if len(select_from) == 0:
        return
    random_issue = random.choice(select_from)
    topic = f'{kind} !4#{random_issue.number}: {random_issue.title}'

    content = f"""
Today I chose {kind} #{random_issue.number} for discussion!

**[{random_issue.title}](https://github.com/leanprover-community/mathlib4/issues/{random_issue.number})**
Created by @**{random_issue.user.name}** (@{random_issue.user.login}) on {random_issue.created_at.date()}
Labels: {', '.join(l.name for l in random_issue.labels)}

Is this {kind} still relevant? Any recent updates? Anyone making progress?
"""

    post = {
        'type'   : 'stream',
        'to'     : 'triage',
        'topic'  : topic,
        'content': content
    }

    print(content)

    zulip_client.send_message(post)

post_random(open_issues, 'issue')
post_random(open_prs, 'PR')
