from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

# Ensure package imports work when tests are discovered as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor_runners.label_management import BorsStatusClient, RunnerLabelManager
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
                _runner(101, "alpha", status="online", busy=False, custom_labels=["bors", "pr"]),
                _runner(102, "beta", status="online", busy=True, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True)

        self.assertEqual(api.remove_calls, [(101, "pr")])
        self.assertEqual(api.add_calls, [])
        self.assertEqual(result.label_errors, "")

    def test_bors_inactive_restores_pr_for_idle_runners_with_bors(self) -> None:
        """Inactive bors should restore `pr` for idle eligible runners.

        Scenario:
        - bors_active is false
        - One online idle runner has `bors` but lacks `pr`.
        - One offline idle runner has `bors` but lacks `pr`.
        - Others are busy, missing `bors`, or already have `pr`.

        Expected behavior:
        - Adds `pr` to both idle runners with `bors` that lack `pr`.
        - Adds missing `bors` only for idle runners.
        """
        payload = _payload(
            [
                _runner(201, "idle-needs-pr", status="online", busy=False, custom_labels=["bors"]),
                _runner(202, "busy-no-bors", status="online", busy=True, custom_labels=[]),
                _runner(203, "idle-no-bors", status="online", busy=False, custom_labels=[]),
                _runner(204, "offline-needs-pr", status="offline", busy=False, custom_labels=["bors"]),
                _runner(205, "idle-has-pr", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=False)

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
                _runner(251, "busy-no-pr", status="online", busy=True, custom_labels=["bors"]),
                _runner(252, "idle-with-pr", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True)

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
                _runner(301, "already-no-pr", status="online", busy=False, custom_labels=["bors"]),
                _runner(302, "with-pr", status="online", busy=False, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True)

        self.assertEqual(api.remove_calls, [])
        self.assertIn("already lacks `pr` label", result.label_summary)

    def test_bors_active_with_no_idle_runner_reports_error(self) -> None:
        """Active bors should error if no idle runner can have `pr` removed.

        Scenario:
        - There is no idle runner that can be selected for `pr` removal.

        Expected behavior:
        - No remove-label mutation occurs.
        - Error output explains no idle runner was available.
        """
        payload = _payload(
            [
                _runner(401, "busy-a", status="online", busy=True, custom_labels=["bors", "pr"]),
                _runner(402, "busy-b", status="offline", busy=True, custom_labels=["bors", "pr"]),
            ]
        )
        api = _FakeRunnerLabelApi()
        manager = RunnerLabelManager(payload=payload, api=api)

        result = manager.apply_policy(bors_active=True)

        self.assertEqual(api.remove_calls, [])
        self.assertIn("No idle runners available", result.label_errors)


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


if __name__ == "__main__":
    unittest.main()
