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
    BorsStatusClient,
    PendingPrJobsClient,
    RunnerLabelManager,
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


class RunnerLabelManagerTests(unittest.TestCase):
    """Unit tests for label policy decisions"""

    def test_bors_active_prefers_first_idle_runner_for_pr_removal(self) -> None:
        """When all runners have `pr`, active bors removes `pr` from a idle runner.

        Scenario:
        - bors_active is true
        - One runner is idle and one is busy.
        - Both already have custom labels `bors,pr`.

        Expected behavior:
        - `pr` is removed from the idle runner.
        - No labels are added.
        """
        payload = _payload(
            [
                _runner(101, "hoskinson1", status="online", busy=False, custom_labels=["bors", "pr"]),
                _runner(102, "hoskinson2", status="online", busy=True, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True, pr_jobs_pending=False)

        self.assertEqual(api.remove_calls, [(101, "pr")])
        self.assertEqual(api.add_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_pending_pr_jobs_restore_pr_for_idle_runners_with_bors(self) -> None:
        """Pending `pr` jobs should restore `pr` for idle eligible runners.

        Scenario:
        - bors_active is false, pr_jobs_pending is true
        - One online idle runner has `bors` but lacks `pr`.
        - One offline idle runner has `bors` but lacks `pr`.
        - Others are busy, missing `bors`, or already have `pr`.

        Expected behavior:
        - Adds `pr` to both idle runners with `bors` that lack `pr`.
        - Adds missing `bors` only for idle runners.
        """
        payload = _payload(
            [
                _runner(201, "hoskinson1-idle-needs-pr", status="online", busy=False, custom_labels=["bors"]),
                _runner(202, "hoskinson2-busy-no-bors", status="online", busy=True, custom_labels=[]),
                _runner(203, "hoskinson3-idle-no-bors", status="online", busy=False, custom_labels=[]),
                _runner(204, "hoskinson4-offline-needs-pr", status="offline", busy=False, custom_labels=["bors"]),
                _runner(205, "hoskinson5-idle-has-pr", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=False, pr_jobs_pending=True)

        self.assertIn((201, "pr"), api.add_calls)
        self.assertIn((204, "pr"), api.add_calls)
        self.assertIn((203, "bors"), api.add_calls)
        self.assertNotIn((202, "bors"), api.add_calls)
        self.assertNotIn((202, "pr"), api.add_calls)
        pr_calls = [call for call in api.add_calls if call == (201, "pr")]
        self.assertEqual(len(pr_calls), 1)
        self.assertEqual(api.remove_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_bors_active_ignores_busy_runner_already_without_pr(self) -> None:
        """Busy runners lacking `pr` should not satisfy the active-bors target.

        Scenario:
        - One busy runner already lacks `pr`.
        - One idle runner still has `pr`.

        Expected behavior:
        - Busy runner is ignored for the active-bors condition.
        - `pr` is removed from the idle runner.
        """
        payload = _payload(
            [
                _runner(251, "hoskinson1-busy-no-pr", status="online", busy=True, custom_labels=["bors"]),
                _runner(252, "hoskinson2-idle-with-pr", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True, pr_jobs_pending=False)

        self.assertEqual(api.remove_calls, [(252, "pr")])
        self.assertEqual(result.label_errors, "")

    def test_bors_active_keeps_existing_runner_without_pr(self) -> None:
        """Active bors should be a no-op if a runner already lacks `pr`.

        Scenario:
        - At least one runner has `bors` but not `pr`.

        Expected behavior:
        - No remove-label mutation is issued.
        - Summary records that an eligible runner already lacked `pr`.
        """
        payload = _payload(
            [
                _runner(301, "hoskinson1-already-no-pr", status="online", busy=False, custom_labels=["bors"]),
                _runner(302, "hoskinson2-with-pr", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True, pr_jobs_pending=False)

        self.assertEqual(api.remove_calls, [])
        self.assertIn("already lacks `pr` label", result.label_summary)

    def test_bors_active_with_no_idle_runner_logs_without_error_summary(self) -> None:
        """Active bors should only log when no idle runner can have `pr` removed.

        Scenario:
        - There is no idle runner that can be selected for `pr` removal.

        Expected behavior:
        - No remove-label mutation occurs.
        """
        payload = _payload(
            [
                _runner(401, "hoskinson1-busy-a", status="online", busy=True, custom_labels=["bors", "pr"]),
                _runner(402, "hoskinson2-busy-b", status="offline", busy=True, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True, pr_jobs_pending=False)

        self.assertEqual(api.remove_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_unmonitored_runners_are_never_relabeled(self) -> None:
        """Runners outside the hoskinson naming scheme must never be mutated.

        Scenario:
        - bors_active is false, pr_jobs_pending is true
        - One idle monitored runner has `bors` but lacks `pr`.
        - Two idle experimental runners (non-hoskinson names) lack labels.

        Expected behavior:
        - Only the monitored runner receives label mutations.
        """
        payload = _payload(
            [
                _runner(501, "hoskinson1", status="online", busy=False, custom_labels=["bors"]),
                _runner(502, "experimental-a", status="online", busy=False, custom_labels=[]),
                _runner(503, "newfleet2-123", status="online", busy=False, custom_labels=["bors"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=False, pr_jobs_pending=True)

        self.assertEqual(api.add_calls, [(501, "pr")])
        self.assertEqual(api.remove_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_unmonitored_runner_does_not_satisfy_active_bors_target(self) -> None:
        """An unmonitored idle runner without `pr` must not count as the free runner.

        Scenario:
        - bors_active is true
        - One idle experimental runner (non-hoskinson name) lacks `pr`.
        - One idle monitored runner still has `pr`.

        Expected behavior:
        - `pr` is removed from the monitored runner.
        """
        payload = _payload(
            [
                _runner(551, "experimental-no-pr", status="online", busy=False, custom_labels=["bors"]),
                _runner(552, "hoskinson1", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True, pr_jobs_pending=False)

        self.assertEqual(api.remove_calls, [(552, "pr")])
        self.assertEqual(api.add_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_only_unmonitored_runners_reports_error(self) -> None:
        """A payload with only unmonitored runners should report a label error.

        Scenario:
        - The payload contains runners, but none match the hoskinson scheme.

        Expected behavior:
        - No mutations occur and an error is reported.
        """
        payload = _payload(
            [
                _runner(601, "experimental-a", status="online", busy=False, custom_labels=[]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=False, pr_jobs_pending=False)

        self.assertEqual(api.add_calls, [])
        self.assertEqual(api.remove_calls, [])
        self.assertIn("No monitored runners found", result.label_errors)

    def test_no_pending_pr_jobs_leaves_pr_labels_unchanged(self) -> None:
        """Without pending `pr` jobs, missing `pr` labels must stay missing.

        Scenario:
        - bors_active is false, pr_jobs_pending is false
        - Two idle runners have `bors` but lack `pr` (the smaller pr pool).

        Expected behavior:
        - No `pr` label is added or removed.
        - Summary records that `pr` labels were left unchanged.
        """
        payload = _payload(
            [
                _runner(701, "hoskinson1", status="online", busy=False, custom_labels=["bors"]),
                _runner(702, "hoskinson2", status="online", busy=False, custom_labels=["bors"]),
                _runner(703, "hoskinson3", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=False, pr_jobs_pending=False)

        self.assertEqual(api.add_calls, [])
        self.assertEqual(api.remove_calls, [])
        self.assertIn("No pending `pr` jobs", result.label_summary)
        self.assertEqual(result.label_errors, "")

    def test_unknown_pending_pr_jobs_reports_error_and_changes_nothing(self) -> None:
        """A failed pending-jobs check must not mutate `pr` labels.

        Scenario:
        - bors_active is false, pr_jobs_pending is None (check failed)
        - One idle runner has `bors` but lacks `pr`.

        Expected behavior:
        - No `pr` label mutation occurs.
        - An error line reports the failed check.
        """
        payload = _payload(
            [
                _runner(801, "hoskinson1", status="online", busy=False, custom_labels=["bors"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=False, pr_jobs_pending=None)

        self.assertEqual(api.add_calls, [])
        self.assertEqual(api.remove_calls, [])
        self.assertIn("could not determine pending `pr` jobs", result.label_errors)

    def test_pending_pr_jobs_with_active_bors_keep_one_runner_reserved(self) -> None:
        """Pending `pr` jobs must not take the runner reserved for bors.

        Scenario:
        - bors_active is true, pr_jobs_pending is true
        - Two idle runners have `bors` but lack `pr`; one idle runner has both.

        Expected behavior:
        - The first idle runner without `pr` stays reserved (no add).
        - The other runner missing `pr` receives it.
        - No `pr` label is removed.
        """
        payload = _payload(
            [
                _runner(901, "hoskinson1", status="online", busy=False, custom_labels=["bors"]),
                _runner(902, "hoskinson2", status="online", busy=False, custom_labels=["bors"]),
                _runner(903, "hoskinson3", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True, pr_jobs_pending=True)

        self.assertEqual(api.add_calls, [(902, "pr")])
        self.assertEqual(api.remove_calls, [])
        self.assertIn("reserved for bors", result.label_summary)
        self.assertEqual(result.label_errors, "")

    def test_pending_pr_jobs_with_active_bors_and_full_pool_only_frees_one(self) -> None:
        """With every idle runner labeled `pr`, active bors still frees one runner.

        Scenario:
        - bors_active is true, pr_jobs_pending is true
        - Both idle runners have `bors` and `pr`.

        Expected behavior:
        - `pr` is removed from the first idle runner (bors reservation).
        - No `pr` label is added; the freed runner stays reserved.
        """
        payload = _payload(
            [
                _runner(951, "hoskinson1", status="online", busy=False, custom_labels=["bors", "pr"]),
                _runner(952, "hoskinson2", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True, pr_jobs_pending=True)

        self.assertEqual(api.remove_calls, [(951, "pr")])
        self.assertEqual(api.add_calls, [])
        self.assertEqual(result.label_errors, "")


class BorsStatusClientTests(unittest.TestCase):
    """Unit tests for bors active-batches API interpretation."""

    def test_has_active_batches_true_for_non_empty_batch_ids(self) -> None:
        """Non-empty batch list should return active=true."""
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            return_value=_FakeHttpResponse({"batch_ids": [123]}),
        ):
            result = BorsStatusClient("https://example.test/api/active-batches").has_active_batches()
        self.assertTrue(result)

    def test_has_active_batches_defaults_true_on_network_error(self) -> None:
        """Network errors should conservatively return active=true."""
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=urllib.error.URLError("network down"),
        ):
            result = BorsStatusClient("https://example.test/api/active-batches").has_active_batches()
        self.assertTrue(result)


def _runs_url(repo: str, status: str) -> str:
    """Build the workflow-runs listing URL used by PendingPrJobsClient."""
    return (
        f"https://api.github.com/repos/test-org/{repo}/actions/runs"
        f"?status={status}&per_page=20"
    )


def _jobs_url(repo: str, run_id: int) -> str:
    """Build the run-jobs URL used by PendingPrJobsClient."""
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


class PendingPrJobsClientTests(unittest.TestCase):
    """Unit tests for queued `pr` job detection."""

    def _client(self) -> PendingPrJobsClient:
        return PendingPrJobsClient(org="test-org", token="token", repos=("repo-a",))

    def test_queued_run_with_queued_pr_job_returns_true(self) -> None:
        """A queued run holding a queued `pr` job should report pending jobs."""
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
            self.assertTrue(self._client().has_pending_pr_jobs())

    def test_in_progress_run_with_queued_pr_job_returns_true(self) -> None:
        """A queued `pr` job inside an in-progress run should also count."""
        responses = {
            _runs_url("repo-a", "queued"): {"workflow_runs": []},
            _runs_url("repo-a", "in_progress"): {"workflow_runs": [{"id": 22}]},
            _jobs_url("repo-a", 22): {
                "jobs": [
                    {"status": "in_progress", "labels": ["ubuntu-latest"]},
                    {"status": "queued", "labels": ["pr"]},
                ]
            },
        }
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=_fake_urlopen_for(responses),
        ):
            self.assertTrue(self._client().has_pending_pr_jobs())

    def test_no_queued_pr_jobs_returns_false(self) -> None:
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
            self.assertIs(self._client().has_pending_pr_jobs(), False)

    def test_listing_failure_returns_none(self) -> None:
        """Network failure on every request should report an unknown result."""
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=urllib.error.URLError("network down"),
        ):
            self.assertIsNone(self._client().has_pending_pr_jobs())

    def test_jobs_failure_without_findings_returns_none(self) -> None:
        """A failed jobs lookup must not turn into a confident `false`."""
        responses = {
            _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 44}]},
            _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
        }
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=_fake_urlopen_for(responses),
        ):
            self.assertIsNone(self._client().has_pending_pr_jobs())


if __name__ == "__main__":
    unittest.main()
