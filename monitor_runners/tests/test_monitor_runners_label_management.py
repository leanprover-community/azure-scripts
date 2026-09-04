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
    busy_fleet_labels,
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


def _busy(*labels: str) -> frozenset[str]:
    """Build a busy-labels set for policy scenarios."""
    return frozenset(labels)


class _FakeRunnerLabelApi:
    """In-memory fake for label mutation calls made by RunnerLabelManager.

    Mutations are exposed as sets: which labels end up added or removed on
    which runner is the policy's contract, the call order is not.
    """

    def __init__(self) -> None:
        self.added: set[tuple[int, str]] = set()
        self.removed: set[tuple[int, str]] = set()

    def add_label(self, runner_id: int, label: str) -> bool:
        """Record add-label call and report success."""
        self.added.add((runner_id, label))
        return True

    def remove_label(self, runner_id: int, label: str) -> bool:
        """Record remove-label call and report success."""
        self.removed.add((runner_id, label))
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


class BusyFleetLabelsTests(unittest.TestCase):
    """Unit tests for the per-label non-hoskinson busy signal."""

    def test_all_runners_with_the_label_busy_reports_label_busy(self) -> None:
        payload = _payload(
            [
                _runner(1, "fleet-a", busy=True, custom_labels=["pr"]),
                _runner(2, "fleet-b", busy=True, custom_labels=["pr", "bors"]),
                _runner(3, "hoskinson1", busy=False),
            ]
        )
        self.assertEqual(busy_fleet_labels(payload), frozenset({"pr", "bors"}))

    def test_idle_runner_clears_only_its_own_labels(self) -> None:
        """An idle `bors` runner must not make the starved `pr` queue look served."""
        payload = _payload(
            [
                _runner(1, "fleet-a", busy=True, custom_labels=["pr"]),
                _runner(2, "fleet-b", busy=False, custom_labels=["bors"]),
            ]
        )
        self.assertEqual(busy_fleet_labels(payload), frozenset({"pr"}))

    def test_idle_runner_carrying_both_labels_clears_both(self) -> None:
        payload = _payload(
            [
                _runner(1, "fleet-a", busy=False, custom_labels=["pr", "bors"]),
            ]
        )
        self.assertEqual(busy_fleet_labels(payload), frozenset())

    def test_idle_offline_runner_does_not_count(self) -> None:
        payload = _payload(
            [
                _runner(1, "fleet-a", busy=True, custom_labels=["pr", "bors"]),
                _runner(2, "fleet-b", status="offline", busy=False, custom_labels=["pr"]),
            ]
        )
        self.assertEqual(busy_fleet_labels(payload), frozenset({"pr", "bors"}))

    def test_label_without_fleet_runners_is_vacuously_busy(self) -> None:
        """A label no online fleet runner carries has no other capacity behind it."""
        payload = _payload(
            [
                _runner(1, "fleet-a", busy=False, custom_labels=["pr"]),
            ]
        )
        self.assertEqual(busy_fleet_labels(payload), frozenset({"bors"}))

    def test_no_fleet_runners_makes_every_label_busy(self) -> None:
        payload = _payload(
            [
                _runner(1, "hoskinson1", busy=False),
                _runner(2, "hoskinson2", busy=False),
            ]
        )
        self.assertEqual(busy_fleet_labels(payload), frozenset({"pr", "bors"}))

    def test_idle_hoskinson_runner_does_not_affect_signal(self) -> None:
        """Standby runners labeled by this policy must not clear their own label."""
        payload = _payload(
            [
                _runner(1, "hoskinson1-20240101", busy=False, custom_labels=["pr", "bors"]),
                _runner(2, "fleet-a", busy=True, custom_labels=["pr", "bors"]),
            ]
        )
        self.assertEqual(busy_fleet_labels(payload), frozenset({"pr", "bors"}))


class RunnerLabelManagerTests(unittest.TestCase):
    """Unit tests for the standby label policy.

    Assertions cover which mutations happen and whether errors are reported;
    summary wording and mutation order are not part of the contract.
    """

    def _apply(
        self,
        runners: list[dict],
        pending_jobs: PendingLabeledJobs,
        busy_labels: frozenset[str],
    ) -> tuple[_FakeRunnerLabelApi, object]:
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=_payload(runners), api=api)
        result = manager.apply_policy(pending_jobs=pending_jobs, busy_labels=busy_labels)
        return api, result

    def test_starved_label_is_added_only_to_idle_online_runners(self) -> None:
        """A starved `pr` queue gets standby capacity from idle online runners.

        Offline, busy, and already-labeled runners are left alone.
        """
        api, result = self._apply(
            [
                _runner(101, "hoskinson1", busy=False, custom_labels=["ephemeral"]),
                _runner(102, "hoskinson2", busy=False, custom_labels=["ephemeral", "pr"]),
                _runner(103, "hoskinson3", status="offline", busy=False, custom_labels=["ephemeral"]),
                _runner(104, "hoskinson4", busy=True, custom_labels=["ephemeral"]),
            ],
            _pending("pr"),
            busy_labels=_busy("pr", "bors"),
        )

        self.assertEqual(api.added, {(101, "pr")})
        self.assertEqual(api.removed, set())
        self.assertFalse(result.label_errors)

    def test_all_starved_labels_are_added(self) -> None:
        """Every pending label whose fleet capacity is busy is handed out."""
        api, result = self._apply(
            [
                _runner(201, "hoskinson1", busy=False, custom_labels=["ephemeral"]),
            ],
            _pending("pr", "bors"),
            busy_labels=_busy("pr", "bors"),
        )

        self.assertEqual(api.added, {(201, "pr"), (201, "bors")})
        self.assertEqual(api.removed, set())
        self.assertFalse(result.label_errors)

    def test_idle_fleet_returns_idle_runners_to_unlabeled_baseline(self) -> None:
        """With idle fleet capacity, idle runners lose all routing labels.

        Old specialty labels are stripped too, offline idle runners included.
        Busy runners and non-hoskinson runners are left alone, and the
        `ephemeral` marker is kept.
        """
        api, result = self._apply(
            [
                _runner(301, "hoskinson1", busy=False, custom_labels=["ephemeral", "pr", "bors"]),
                _runner(302, "hoskinson7", status="offline", busy=False, custom_labels=["bors", "doc-gen"]),
                _runner(303, "hoskinson9", busy=True, custom_labels=["bors", "lean4checker"]),
                _runner(304, "fleet-a", busy=False, custom_labels=["pr", "bors"]),
            ],
            _pending("pr"),
            busy_labels=_busy(),
        )

        self.assertEqual(api.added, set())
        self.assertEqual(
            api.removed,
            {(301, "pr"), (301, "bors"), (302, "bors"), (302, "doc-gen")},
        )
        self.assertFalse(result.label_errors)

    def test_non_starved_labels_are_stripped_while_starved_ones_are_added(self) -> None:
        """Only the starved label stays; the other standby label goes away."""
        api, result = self._apply(
            [
                _runner(401, "hoskinson1", busy=False, custom_labels=["ephemeral", "bors"]),
            ],
            _pending("pr"),
            busy_labels=_busy("pr", "bors"),
        )

        self.assertEqual(api.added, {(401, "pr")})
        self.assertEqual(api.removed, {(401, "bors")})
        self.assertFalse(result.label_errors)

    def test_label_with_idle_fleet_capacity_is_not_handed_to_standby(self) -> None:
        """A queued label whose own fleet runners are idle must stay unlabeled.

        `pr` is starved and gets standby capacity; `bors` still has idle fleet
        runners, so the standby runner loses its leftover `bors` label.
        """
        api, result = self._apply(
            [
                _runner(451, "hoskinson1", busy=False, custom_labels=["ephemeral", "bors"]),
            ],
            _pending("pr", "bors"),
            busy_labels=_busy("pr"),
        )

        self.assertEqual(api.added, {(451, "pr")})
        self.assertEqual(api.removed, {(451, "bors")})
        self.assertFalse(result.label_errors)

    def test_busy_labels_without_pending_jobs_keep_standby_unlabeled(self) -> None:
        """Busy fleet labels with an empty queue are not a reason to label standby."""
        api, result = self._apply(
            [
                _runner(501, "hoskinson1", busy=False, custom_labels=["ephemeral", "pr"]),
            ],
            _pending(),
            busy_labels=_busy("pr", "bors"),
        )

        self.assertEqual(api.added, set())
        self.assertEqual(api.removed, {(501, "pr")})
        self.assertFalse(result.label_errors)

    def test_incomplete_check_never_removes_labels(self) -> None:
        """An incomplete pending-jobs check must not strip any label.

        A label missing from the pending set may still have queued jobs, so
        nothing is removed and an error is reported.
        """
        api, result = self._apply(
            [
                _runner(601, "hoskinson1", busy=False, custom_labels=["ephemeral", "pr"]),
            ],
            _pending(check_failed=True),
            busy_labels=_busy("pr", "bors"),
        )

        self.assertEqual(api.added, set())
        self.assertEqual(api.removed, set())
        self.assertTrue(result.label_errors)

    def test_incomplete_check_still_adds_labels_found_pending(self) -> None:
        """Labels found pending are definite positives even when the check failed."""
        api, result = self._apply(
            [
                _runner(701, "hoskinson1", busy=False, custom_labels=["ephemeral", "bors"]),
            ],
            _pending("pr", check_failed=True),
            busy_labels=_busy("pr", "bors"),
        )

        self.assertEqual(api.added, {(701, "pr")})
        self.assertEqual(api.removed, set())
        self.assertTrue(result.label_errors)

    def test_draining_runner_is_never_mutated(self) -> None:
        """A runner carrying `cleanup-soon` must keep its labels untouched."""
        api, result = self._apply(
            [
                _runner(
                    801,
                    "hoskinson1",
                    busy=False,
                    custom_labels=["ephemeral", "cleanup-soon", "pr"],
                ),
            ],
            _pending("bors"),
            busy_labels=_busy("pr", "bors"),
        )

        self.assertEqual(api.added, set())
        self.assertEqual(api.removed, set())
        self.assertFalse(result.label_errors)

    def test_unmonitored_runners_are_never_mutated(self) -> None:
        """Runners outside the hoskinson naming scheme must never be mutated."""
        api, result = self._apply(
            [
                _runner(901, "fleet-a", busy=False, custom_labels=["bors"]),
                _runner(902, "hoskinson1", busy=False, custom_labels=["ephemeral"]),
            ],
            _pending("pr"),
            busy_labels=_busy("pr"),
        )

        self.assertEqual(api.added, {(902, "pr")})
        self.assertEqual(api.removed, set())
        self.assertFalse(result.label_errors)

    def test_only_unmonitored_runners_reports_error(self) -> None:
        """A payload with only unmonitored runners must report an error."""
        api, result = self._apply(
            [
                _runner(1001, "experimental-a", busy=False, custom_labels=[]),
            ],
            _pending(),
            busy_labels=_busy(),
        )

        self.assertEqual(api.added, set())
        self.assertEqual(api.removed, set())
        self.assertTrue(result.label_errors)


class RenderPendingLabelsTests(unittest.TestCase):
    """Unit tests for pending-labels output rendering."""

    def test_pending_labels_appear_in_rendered_text(self) -> None:
        text = render_pending_labels(_pending("pr", "bors"))
        self.assertIn("pr", text)
        self.assertIn("bors", text)

    def test_nothing_pending_and_unknown_render_differently(self) -> None:
        """An empty result must not look like a failed check."""
        self.assertNotEqual(
            render_pending_labels(_pending()),
            render_pending_labels(_pending(check_failed=True)),
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

    def _pending_labels(self, responses: dict[str, dict]) -> PendingLabeledJobs:
        client = PendingLabeledJobsClient(org="test-org", token="token", repos=("repo-a",))
        with patch(
            "monitor_runners.label_management.urllib.request.urlopen",
            side_effect=_fake_urlopen_for(responses),
        ):
            return client.pending_labels()

    def test_queued_run_with_queued_pr_job_reports_pr_pending(self) -> None:
        """A queued run holding a queued `pr` job should report `pr` pending."""
        result = self._pending_labels(
            {
                _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 11}]},
                _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
                _jobs_url("repo-a", 11): {
                    "jobs": [
                        {"status": "completed", "labels": ["ubuntu-latest"]},
                        {"status": "queued", "labels": ["pr"]},
                    ]
                },
            }
        )
        self.assertEqual(result.pending, frozenset({"pr"}))
        self.assertFalse(result.check_failed)

    def test_multiple_standby_labels_are_collected_across_runs(self) -> None:
        """`pr` and `bors` jobs in different runs should both be reported."""
        result = self._pending_labels(
            {
                _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 21}]},
                _runs_url("repo-a", "in_progress"): {"workflow_runs": [{"id": 22}]},
                _jobs_url("repo-a", 21): {"jobs": [{"status": "queued", "labels": ["bors"]}]},
                _jobs_url("repo-a", 22): {"jobs": [{"status": "queued", "labels": ["pr"]}]},
            }
        )
        self.assertEqual(result.pending, frozenset({"pr", "bors"}))
        self.assertFalse(result.check_failed)

    def test_no_queued_standby_jobs_reports_empty(self) -> None:
        """Runs whose jobs run elsewhere or already started should not count."""
        result = self._pending_labels(
            {
                _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 33}]},
                _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
                _jobs_url("repo-a", 33): {
                    "jobs": [
                        {"status": "queued", "labels": ["ubuntu-latest"]},
                        {"status": "in_progress", "labels": ["pr"]},
                    ]
                },
            }
        )
        self.assertEqual(result.pending, frozenset())
        self.assertFalse(result.check_failed)

    def test_listing_failure_reports_incomplete_check(self) -> None:
        """Network failure on every request should mark the check incomplete."""
        result = self._pending_labels({})
        self.assertEqual(result.pending, frozenset())
        self.assertTrue(result.check_failed)

    def test_jobs_failure_without_findings_reports_incomplete_check(self) -> None:
        """A failed jobs lookup must not turn into a confident empty result."""
        result = self._pending_labels(
            {
                _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 44}]},
                _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
            }
        )
        self.assertEqual(result.pending, frozenset())
        self.assertTrue(result.check_failed)

    def test_jobs_failure_with_findings_keeps_found_labels(self) -> None:
        """Labels already found stay pending even when another lookup fails."""
        result = self._pending_labels(
            {
                _runs_url("repo-a", "queued"): {"workflow_runs": [{"id": 55}, {"id": 56}]},
                _runs_url("repo-a", "in_progress"): {"workflow_runs": []},
                _jobs_url("repo-a", 55): {"jobs": [{"status": "queued", "labels": ["pr"]}]},
            }
        )
        self.assertEqual(result.pending, frozenset({"pr"}))
        self.assertTrue(result.check_failed)


if __name__ == "__main__":
    unittest.main()
