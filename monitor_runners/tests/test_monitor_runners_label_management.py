from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

# Ensure package imports work when tests are discovered as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor_runners.label_management import (
    PendingLabeledJobs,
    PendingLabeledJobsClient,
    RunnerLabelManager,
    fleet_is_busy,
    render_pending_labels,
)
from monitor_runners.models import GitHubRunnersPayload


def _runner(
    runner_id: int,
    name: str,
    *,
    status: str = "online",
    busy: bool = False,
    custom_labels: list[str] | None = None,
) -> dict:
    """Build one runner payload entry for label-management scenarios."""
    labels = [{"name": "self-hosted", "type": "read-only"}]
    for label in custom_labels or []:
        labels.append({"name": label, "type": "custom"})
    return {
        "id": runner_id,
        "name": name,
        "status": status,
        "busy": busy,
        "os": "Linux",
        "labels": labels,
    }


def _payload(runners: list[dict]) -> GitHubRunnersPayload:
    """Build typed payload objects from plain runner dictionaries."""
    return GitHubRunnersPayload.from_dict({"total_count": len(runners), "runners": runners})


def _pending(*labels: str, check_failed: bool = False) -> PendingLabeledJobs:
    """Build a pending-jobs result for policy scenarios."""
    return PendingLabeledJobs(pending=frozenset(labels), check_failed=check_failed)


class _FakeRunnerLabelApi:
    """In-memory fake for label mutation calls made by RunnerLabelManager."""

    def __init__(self) -> None:
        self.add_calls: list[tuple[int, str]] = []
        self.remove_calls: list[tuple[int, str]] = []

    def add_label(self, runner_id: int, label: str) -> bool:
        """Record add-label call and report success."""
        self.add_calls.append((runner_id, label))
        return True

    def remove_label(self, runner_id: int, label: str) -> bool:
        """Record remove-label call and report success."""
        self.remove_calls.append((runner_id, label))
        return True


class _FakeHttpResponse:
    """Simple context-manager HTTP response object for urlopen patches."""

    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        """Return encoded payload body."""
        return self._raw


class FleetIsBusyTests(unittest.TestCase):
    """Unit tests for the non-hoskinson fleet busy signal."""

    def test_all_online_fleet_runners_busy_is_true(self) -> None:
        payload = _payload(
            [
                _runner(1, "fleet-a", busy=True),
                _runner(2, "fleet-b", busy=True),
                _runner(3, "hoskinson1", busy=False),
            ]
        )
        self.assertTrue(fleet_is_busy(payload))

    def test_one_idle_online_fleet_runner_is_false(self) -> None:
        payload = _payload(
            [
                _runner(1, "fleet-a", busy=True),
                _runner(2, "fleet-b", busy=False),
            ]
        )
        self.assertFalse(fleet_is_busy(payload))

    def test_idle_offline_fleet_runner_does_not_count(self) -> None:
        payload = _payload(
            [
                _runner(1, "fleet-a", busy=True),
                _runner(2, "fleet-b", status="offline", busy=False),
            ]
        )
        self.assertTrue(fleet_is_busy(payload))

    def test_no_fleet_runners_is_vacuously_true(self) -> None:
        payload = _payload(
            [
                _runner(1, "hoskinson1", busy=False),
                _runner(2, "hoskinson2", busy=False),
            ]
        )
        self.assertTrue(fleet_is_busy(payload))

    def test_idle_hoskinson_runner_does_not_affect_signal(self) -> None:
        payload = _payload(
            [
                _runner(1, "hoskinson1-20240101", busy=False),
                _runner(2, "fleet-a", busy=True),
            ]
        )
        self.assertTrue(fleet_is_busy(payload))


class RunnerLabelManagerTests(unittest.TestCase):
    """Unit tests for the standby label policy."""

    def test_fleet_busy_and_pending_pr_adds_pr_to_idle_online_runners(self) -> None:
        """A starved `pr` queue should get standby capacity.

        Scenario:
        - Fleet is busy and queued jobs request `pr`.
        - One idle online runner lacks `pr`, one already has it.
        - One idle offline runner and one busy runner also lack `pr`.

        Expected behavior:
        - `pr` is added only to the idle online runner lacking it.
        - No removals occur (`pr` is active).
        """
        payload = _payload(
            [
                _runner(101, "hoskinson1", busy=False, custom_labels=["ephemeral"]),
                _runner(102, "hoskinson2", busy=False, custom_labels=["ephemeral", "pr"]),
                _runner(103, "hoskinson3", status="offline", busy=False, custom_labels=["ephemeral"]),
                _runner(104, "hoskinson4", busy=True, custom_labels=["ephemeral"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(pending_jobs=_pending("pr"), fleet_busy=True)

        self.assertEqual(api.add_calls, [(101, "pr")])
        self.assertEqual(api.remove_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_fleet_busy_with_both_labels_pending_adds_both(self) -> None:
        """Both standby labels get added when both queues are starved."""
        payload = _payload(
            [
                _runner(201, "hoskinson1", busy=False, custom_labels=["ephemeral"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(pending_jobs=_pending("pr", "bors"), fleet_busy=True)

        self.assertEqual(api.add_calls, [(201, "pr"), (201, "bors")])
        self.assertEqual(api.remove_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_idle_fleet_strips_routing_labels_from_idle_runners(self) -> None:
        """With idle fleet capacity, idle hoskinson runners return to the unlabeled baseline.

        Scenario:
        - Fleet is not busy; queued jobs request `pr` (fleet will take them).
        - Idle runners hold old routing labels, including specialty labels.
        - One busy runner holds `pr`.

        Expected behavior:
        - Routing labels are removed from idle runners (offline included).
        - The busy runner is not mutated; the `ephemeral` marker is kept.
        """
        payload = _payload(
            [
                _runner(301, "hoskinson1", busy=False, custom_labels=["ephemeral", "pr", "bors"]),
                _runner(302, "hoskinson7", status="offline", busy=False, custom_labels=["bors", "doc-gen"]),
                _runner(303, "hoskinson9", busy=True, custom_labels=["bors", "lean4checker"]),
                _runner(304, "fleet-a", busy=False, custom_labels=["pr", "bors"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(pending_jobs=_pending("pr"), fleet_busy=False)

        self.assertEqual(api.add_calls, [])
        self.assertEqual(
            api.remove_calls,
            [(301, "pr"), (301, "bors"), (302, "bors"), (302, "doc-gen")],
        )
        self.assertEqual(result.label_errors, "")

    def test_fleet_busy_with_pending_pr_strips_inactive_bors(self) -> None:
        """Only the starved label stays; the other standby label is stripped."""
        payload = _payload(
            [
                _runner(401, "hoskinson1", busy=False, custom_labels=["ephemeral", "bors"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(pending_jobs=_pending("pr"), fleet_busy=True)

        self.assertEqual(api.add_calls, [(401, "pr")])
        self.assertEqual(api.remove_calls, [(401, "bors")])
        self.assertEqual(result.label_errors, "")

    def test_fleet_busy_without_pending_jobs_only_strips(self) -> None:
        """A busy fleet with an empty queue keeps the standby pool unlabeled."""
        payload = _payload(
            [
                _runner(501, "hoskinson1", busy=False, custom_labels=["ephemeral", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(pending_jobs=_pending(), fleet_busy=True)

        self.assertEqual(api.add_calls, [])
        self.assertEqual(api.remove_calls, [(501, "pr")])
        self.assertIn("no pending standby jobs", result.label_summary)

    def test_incomplete_check_without_findings_holds_all_labels(self) -> None:
        """An incomplete pending-jobs check must not strip any label.

        A label missing from the pending set may still have queued jobs, so
        removals are skipped and an error line is reported.
        """
        payload = _payload(
            [
                _runner(601, "hoskinson1", busy=False, custom_labels=["ephemeral", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(
            pending_jobs=_pending(check_failed=True), fleet_busy=True
        )

        self.assertEqual(api.add_calls, [])
        self.assertEqual(api.remove_calls, [])
        self.assertIn("pending-jobs check incomplete", result.label_errors)

    def test_incomplete_check_with_findings_still_adds_labels(self) -> None:
        """Labels found pending are definite positives even when the check failed."""
        payload = _payload(
            [
                _runner(701, "hoskinson1", busy=False, custom_labels=["ephemeral", "bors"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(
            pending_jobs=_pending("pr", check_failed=True), fleet_busy=True
        )

        self.assertEqual(api.add_calls, [(701, "pr")])
        self.assertEqual(api.remove_calls, [])
        self.assertIn("pending-jobs check incomplete", result.label_errors)

    def test_unmonitored_runners_are_never_mutated(self) -> None:
        """Runners outside the hoskinson naming scheme must never be mutated."""
        payload = _payload(
            [
                _runner(801, "fleet-a", busy=False, custom_labels=["pr", "bors"]),
                _runner(802, "hoskinson1", busy=False, custom_labels=["ephemeral"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(pending_jobs=_pending("pr"), fleet_busy=True)

        self.assertEqual(api.add_calls, [(802, "pr")])
        self.assertEqual(api.remove_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_draining_runner_is_never_mutated(self) -> None:
        """A runner carrying `cleanup-soon` must keep its labels untouched.

        Scenario:
        - One idle online runner is draining for disk cleanup and holds `pr`.
        - Fleet is busy and `pr` and `bors` jobs are pending.

        Expected behavior:
        - No label is added to or removed from the draining runner.
        """
        payload = _payload(
            [
                _runner(
                    1001,
                    "hoskinson1",
                    busy=False,
                    custom_labels=["ephemeral", "cleanup-soon", "pr"],
                ),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(pending_jobs=_pending("bors"), fleet_busy=True)

        self.assertEqual(api.add_calls, [])
        self.assertEqual(api.remove_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_only_unmonitored_runners_reports_error(self) -> None:
        """A payload with only unmonitored runners should report a label error."""
        payload = _payload(
            [
                _runner(901, "experimental-a", busy=False, custom_labels=[]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(pending_jobs=_pending(), fleet_busy=False)

        self.assertEqual(api.add_calls, [])
        self.assertEqual(api.remove_calls, [])
        self.assertIn("No monitored runners found", result.label_errors)


class RenderPendingLabelsTests(unittest.TestCase):
    """Unit tests for pending-labels output rendering."""

    def test_render_variants(self) -> None:
        self.assertEqual(render_pending_labels(_pending()), "none")
        self.assertEqual(render_pending_labels(_pending("pr")), "pr")
        self.assertEqual(render_pending_labels(_pending("pr", "bors")), "bors,pr")
        self.assertEqual(render_pending_labels(_pending(check_failed=True)), "unknown")
        self.assertEqual(
            render_pending_labels(_pending("pr", check_failed=True)),
            "pr (check incomplete)",
        )


def _runs_url(repo: str, status: str) -> str:
    """Build the workflow-runs listing URL used by PendingLabeledJobsClient."""
    return (
        f"https://api.github.com/repos/test-org/{repo}/actions/runs"
        f"?status={status}&per_page=20"
    )


def _jobs_url(repo: str, run_id: int) -> str:
    """Build the run-jobs URL used by PendingLabeledJobsClient."""
    return (
        f"https://api.github.com/repos/test-org/{repo}/actions/runs/{run_id}/jobs"
        f"?filter=latest&per_page=100"
    )


def _fake_urlopen_for(responses: dict[str, dict]):
    """Return an urlopen stub serving canned payloads keyed by full URL."""

    def fake_urlopen(request, timeout=0):
        url = request.full_url
        if url not in responses:
            raise urllib.error.URLError(f"unexpected URL: {url}")
        return _FakeHttpResponse(responses[url])

    return fake_urlopen


class PendingLabeledJobsClientTests(unittest.TestCase):
    """Unit tests for queued standby-label job detection."""

    def _client(self) -> PendingLabeledJobsClient:
        return PendingLabeledJobsClient(org="test-org", token="token", repos=("repo-a",))

    def test_queued_run_with_queued_pr_job_reports_pr_pending(self) -> None:
        """A queued run holding a queued `pr` job should report `pr` pending."""
        responses = {
            _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 11}]},
            _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
            _jobs_url("repo-a", 11): {
                "jobs": [
                    {"status": "completed", "labels": ["ubuntu-latest"]},
                    {"status": "queued", "labels": ["pr"]},
                ]
            },
        }
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=_fake_urlopen_for(responses),
        ):
            result = self._client().pending_labels()
        self.assertEqual(result.pending, frozenset({"pr"}))
        self.assertFalse(result.check_failed)

    def test_multiple_standby_labels_are_collected_across_runs(self) -> None:
        """`pr` and `bors` jobs in different runs should both be reported."""
        responses = {
            _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 21}]},
            _runs_url("repo-a", "in_progress"): {"workflow_runs": [{"id": 22}]},
            _jobs_url("repo-a", 21): {
                "jobs": [{"status": "queued", "labels": ["bors"]}]
            },
            _jobs_url("repo-a", 22): {
                "jobs": [{"status": "queued", "labels": ["pr"]}]
            },
        }
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=_fake_urlopen_for(responses),
        ):
            result = self._client().pending_labels()
        self.assertEqual(result.pending, frozenset({"pr", "bors"}))
        self.assertFalse(result.check_failed)

    def test_no_queued_standby_jobs_reports_empty(self) -> None:
        """Runs whose jobs run elsewhere or already started should not count."""
        responses = {
            _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 33}]},
            _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
            _jobs_url("repo-a", 33): {
                "jobs": [
                    {"status": "queued", "labels": ["ubuntu-latest"]},
                    {"status": "in_progress", "labels": ["pr"]},
                ]
            },
        }
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=_fake_urlopen_for(responses),
        ):
            result = self._client().pending_labels()
        self.assertEqual(result.pending, frozenset())
        self.assertFalse(result.check_failed)

    def test_listing_failure_reports_incomplete_check(self) -> None:
        """Network failure on every request should mark the check incomplete."""
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=urllib.error.URLError("network down"),
        ):
            result = self._client().pending_labels()
        self.assertEqual(result.pending, frozenset())
        self.assertTrue(result.check_failed)

    def test_jobs_failure_without_findings_reports_incomplete_check(self) -> None:
        """A failed jobs lookup must not turn into a confident empty result."""
        responses = {
            _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 44}]},
            _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
        }
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=_fake_urlopen_for(responses),
        ):
            result = self._client().pending_labels()
        self.assertEqual(result.pending, frozenset())
        self.assertTrue(result.check_failed)

    def test_jobs_failure_with_findings_keeps_found_labels(self) -> None:
        """Labels already found stay pending even when another lookup fails."""
        responses = {
            _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 55}, {"id": 56}]},
            _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
            _jobs_url("repo-a", 55): {
                "jobs": [{"status": "queued", "labels": ["pr"]}]
            },
        }
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=_fake_urlopen_for(responses),
        ):
            result = self._client().pending_labels()
        self.assertEqual(result.pending, frozenset({"pr"}))
        self.assertTrue(result.check_failed)


if __name__ == "__main__":
    unittest.main()
