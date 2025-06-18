# azure-scripts


This repo contains cron jobs for the following [leanprover-community](https://leanprover-community.github.io) task:
- the triage Zulip bot: posts a random issue and PR to the [Lean Zulip](https://leanprover.zulipchat.com/#narrow/channel/263328-triage) daily

The following tasks / workflows are outdated and no longer run:
- update [mathlib](https://github.com/leanprover-community/mathlib)'s `lean-3.x.y` branch
- update mathlib's [linting](https://arxiv.org/abs/2004.03673) exception files
- delete stale olean archives from Azure
- update the [Lean+mathlib+vscodium bundles](https://leanprover-community.github.io/get_started.html#maybe-a-couple-of-nights)

## periodic bumps

The cron job in this repo use to stop periodically without attention, but the [liskin/gh-workflow-keepalive](https://github.com/liskin/gh-workflow-keepalive) should keep it working from now on.
