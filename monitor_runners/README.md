# monitor_runners

Python package for self-hosted runner monitoring and weekly reporting.

## What it contains

- `core.py`: runner state machine, transition detection, alert planning, and orchestration.
- `models.py`: typed data models/enums for GitHub payloads, state, and stats.
- `label_management.py`: standby runner label policy and GitHub label mutation client. The policy labels idle hoskinson runners only when every non-hoskinson runner with that label is busy and jobs are queued behind it. It adds a standby label only after 10 consecutive checks find that label starved, so burst capacity serves the surge first. It then keeps that label for 10 more checks after starvation stops, so an intermittently starved queue holds its standby capacity.
- `reporting.py`: weekly markdown report generation from stats.
- `workflow.py`: GitHub Actions CLI entrypoints:
  - `check-runners`
  - `manage-labels`
  - `weekly-report`
  - `label-loop`: repeats label management on an interval (default 30s) until a
    deadline; the workflow runs it in a dedicated `label-management-loop` job so
    each run keeps managing labels until the next scheduled run cancels it
    (`concurrency.cancel-in-progress`).

## Run tests
All tests:

```bash
python3 -m unittest discover -s tests -v
```

Specific files:

```bash
python3 -m unittest -v tests/test_monitor_runners.py
python3 -m unittest -v tests/test_monitor_runners_core_objects.py
python3 -m unittest -v tests/test_monitor_runners_label_management.py
python3 -m unittest -v tests/test_monitor_runners_workflow.py
python3 -m unittest -v tests/test_monitor_runners_label_management_workflow.py
```

Specific test case or method:

```bash
python3 -m unittest -v tests.test_monitor_runners_workflow.WorkflowErrorNotificationTests
python3 -m unittest -v tests.test_monitor_runners_core_objects.HostStateMachineTests.test_missing_twice_transitions_to_absent_once
```
