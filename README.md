# azure-scripts

This repo contains cron jobs for the following [leanprover-community](https://leanprover-community.github.io) task:
- the triage Zulip bot: posts a random issue and PR to the [Lean Zulip](https://leanprover.zulipchat.com/#narrow/channel/263328-triage) daily
- blog and website report bot: posts a summary of open PRs in the [website repo](https://github.com/leanprover-community/leanprover-community.github.io) and [blog repo](https://github.com/leanprover-community/blog) to the [Lean Zulip](https://leanprover.zulipchat.com/#narrow/channel/263328-triage) weekly
- the self-hosted runner monitor: posts a message to the (private) CI Admins zulip channel if any of the leanprover-commuity self-hosted runners is offline. Runs every 15 minutes.

The following tasks / workflows are outdated and no longer run:
- update [mathlib](https://github.com/leanprover-community/mathlib)'s `lean-3.x.y` branch
- update mathlib's [linting](https://arxiv.org/abs/2004.03673) exception files
- delete stale olean archives from Azure
- update the Lean+mathlib+vscodium bundles

## periodic bumps

The cron jobs in this repo used to stop periodically without attention, but the [liskin/gh-workflow-keepalive](https://github.com/liskin/gh-workflow-keepalive) should keep them working from now on.
