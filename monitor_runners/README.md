# monitor_runners

Python package for self-hosted runner monitoring and weekly reporting.

## What it contains

- `core.py`: runner state machine, transition detection, alert planning, and orchestration.
- `models.py`: typed data models/enums for GitHub payloads, state, and stats.
- `reporting.py`: weekly markdown report generation from stats.
- `workflow.py`: GitHub Actions CLI entrypoints:
  - `check-runners`
  - `weekly-report`

## Run tests
All tests:

```bash
python3 -m unittest discover -s tests -v
```

Specific files:

```bash
python3 -m unittest -v tests/test_monitor_runners.py
python3 -m unittest -v tests/test_monitor_runners_core_objects.py
python3 -m unittest -v tests/test_monitor_runners_workflow.py
```

Specific test case or method:

```bash
python3 -m unittest -v tests.test_monitor_runners_workflow.WorkflowErrorNotificationTests
python3 -m unittest -v tests.test_monitor_runners_core_objects.HostStateMachineTests.test_missing_twice_transitions_to_absent_once
```
