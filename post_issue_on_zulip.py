#!/usr/bin/env python3

import sys
import zulip
import github
import datetime
import random
import re

zulip_token = sys.argv[1]
gh_token = sys.argv[2]

zulip_client = zulip.Client(email="random-issue-bot@zulipchat.com", api_key=zulip_token, site="https://leanprover.zulipchat.com")

def message_date(id):
    return zulip_client.get_message_history(id)['message_history'][0]['timestamp']

posted_topics = zulip_client.get_stream_topics(zulip_client.get_stream_id('triage')['stream_id'])['topics']
print(posted_topics)
pattern = re.compile(r'\(#(\d+)\)$')
posted_topics = {v[0]: message_date(t['max_id']) for (t, v) in ((t, pattern.findall(t['name'])) for t in posted_topics) if len(v) > 0}

print(posted_topics)

gh = github.Github(login_or_token=gh_token)
mathlib = gh.get_repo('leanprover-community/mathlib')

open_items = mathlib.get_issues(state='open')
open_prs = []
open_issues = []

for i in open_items:
    now = datetime.datetime.now()
    delta = datetime.timedelta(days=14)
    min_age = now - delta
    if i.created_at < min_age \
         and 'blocked-by-other-PR' not in [l.name for l in i.labels] \
             and not (i.number in posted_topics and datetime.datetime.fromtimestamp(posted_topics[i.number]) > min_age):
        if i.pull_request:
            open_prs.append(i)
        else:
            open_issues.append(i)


quit()

def post_random(select_from, kind):
    random_issue = random.choice(select_from)
    topic = f'{kind} #{random_issue.number}: {random_issue.title}'

    content = f"""
Today I chose {kind} {random_issue.number} for discussion!

**[{random_issue.title}](https://github.com/leanprover-community/mathlib/issues/{random_issue.number})**
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
