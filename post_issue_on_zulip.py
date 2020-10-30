#!/usr/bin/env python3

import sys
import zulip
import github
import datetime
import random

zulip_token = sys.argv[1]
gh_token = sys.argv[2]

zulip_client = zulip.Client(email="random-issue-bot@zulipchat.com", api_key=zulip_token, site="https://leanprover.zulipchat.com")

gh = github.Github(login_or_token=gh_token)
mathlib = gh.get_repo('leanprover-community/mathlib')

open_items = mathlib.get_issues(state='open')
open_prs = []
open_issues = []

for i in open_items:
    if i.created_at < datetime.datetime.now() - datetime.timedelta(days=14) and 'blocked-by-other-PR' not in [l.name for l in i.labels]:
        if i.pull_request:
            open_prs.append(i)
        else:
            open_issues.append(i)

def post_random(select_from, kind):
    random_issue = random.choice(select_from)
    topic = f'random {kind}: {random_issue.title} (#{random_issue.number})'

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
