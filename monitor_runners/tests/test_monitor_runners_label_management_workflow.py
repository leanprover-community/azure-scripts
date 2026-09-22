from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure package imports work when tests are discovered as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor_runners.label_management import (
    ADD_ITERATIONS,
    KEEP_ITERATIONS,
    LabelManagementResult,
    PendingLabeledJobs,
    StandbyLabelHysteresis,
)
from monitor_runners.workflow import main


def _parse_github_output(path: Path) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file with key=value and <<EOF entries."""
    result: dict[str, str] = {}
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<EOF" in line:
            key = line.split("<<EOF", 1)[0]
            i += 1
            content_lines = []
            while i < len(lines) and lines[i] != "EOF":
                content_lines.append(lines[i])
                i += 1
            result[key] = "\n".join(content_lines)
            i += 1
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
        i += 1
    return result


def _write_payload(path: Path, labels: tuple[str, ...] = ("bors", "pr")) -> None:
    """Write the minimal runners payload file used by CLI integration tests."""
    payload = {
        "total_count": 1,
        "runners": [
            {
                "id": 1,
                "name": "hoskinson1",
                "status": "online",
                "busy": False,
                "os": "Linux",
                "labels": [{"name": label, "type": "custom"} for label in labels],
            }
        ],
    }
    path.write_text(json.dumps(payload))


class WorkflowLabelManagementIntegrationTests(unittest.TestCase):
    """Integration tests for workflow.py `manage-labels` command I/O wiring."""

    def test_manage_labels_writes_outputs_from_service_result(self) -> None:
        """Workflow CLI should emit GITHUB_OUTPUT values from label-management result.

        Scenario:
        - `execute_label_management` is stubbed to return known result values.
        - command runs against a valid response-file path.

        Expected behavior:
        - command exits 0.
        - outputs include pending_labels/busy_labels/label_summary/label_errors/has_label_errors.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_file = tmp_path / "runners_response.json"
            output_file = tmp_path / "github_output.txt"
            _write_payload(response_file)

            with patch(
                "monitor_runners.workflow.execute_label_management",
                return_value=LabelManagementResult(
                    pending_labels="pr",
                    busy_labels="pr",
                    label_summary="Added `pr` label to runner `alpha`",
                    label_errors="Failed to remove `pr` label from runner `beta`",
                ),
            ):
                rc = main(
                    [
                        "manage-labels",
                        "--token",
                        "token",
                        "--org",
                        "leanprover-community",
                        "--response-file",
                        str(response_file),
                        "--dry-run",
                        "false",
                        "--github-output",
                        str(output_file),
                    ]
                )

            self.assertEqual(rc, 0)
            outputs = _parse_github_output(output_file)
            self.assertEqual(outputs.get("pending_labels"), "pr")
            self.assertEqual(outputs.get("busy_labels"), "pr")
            self.assertEqual(outputs.get("has_label_errors"), "true")
            self.assertIn("Added `pr`", outputs.get("label_summary", ""))
            self.assertIn("Failed to remove `pr`", outputs.get("label_errors", ""))

    def test_manage_labels_passes_dry_run_and_cleans_response_file(self) -> None:
        """Workflow CLI should pass parsed dry_run and remove temporary response file.

        Scenario:
        - command is called with `--dry-run true`.
        - label-management execution is mocked.

        Expected behavior:
        - mocked service receives `dry_run=True`.
        - response file is removed as post-step cleanup.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_file = tmp_path / "runners_response.json"
            output_file = tmp_path / "github_output.txt"
            _write_payload(response_file)

            with patch(
                "monitor_runners.workflow.execute_label_management",
                return_value=LabelManagementResult(
                    pending_labels="unknown",
                    busy_labels="none",
                    label_summary="",
                    label_errors="",
                ),
            ) as execute:
                rc = main(
                    [
                        "manage-labels",
                        "--token",
                        "token",
                        "--org",
                        "leanprover-community",
                        "--response-file",
                        str(response_file),
                        "--dry-run",
                        "true",
                        "--github-output",
                        str(output_file),
                    ]
                )

            self.assertEqual(rc, 0)
            self.assertFalse(response_file.exists())
            self.assertTrue(execute.called)
            self.assertIs(execute.call_args.kwargs.get("dry_run"), True)
            outputs = _parse_github_output(output_file)
            self.assertEqual(outputs.get("pending_labels"), "unknown")
            self.assertEqual(outputs.get("busy_labels"), "none")

    def test_manage_labels_dry_run_summary_has_clear_prefix(self) -> None:
        """Dry-run mode should prefix summary while keeping normal mutation wording.

        Scenario:
        - an idle runner carries the `doc-gen` specialty label, which has no
          grace period.
        - command runs with `--dry-run true` on the real execution path.

        Expected behavior:
        - summary is marked as a dry run.
        - summary still describes the would-be mutations.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_file = tmp_path / "runners_response.json"
            output_file = tmp_path / "github_output.txt"
            _write_payload(response_file, labels=("doc-gen",))

            with patch(
                "monitor_runners.label_management.PendingLabeledJobsClient.pending_labels",
                return_value=PendingLabeledJobs(pending=frozenset(), check_failed=False),
            ):
                rc = main(
                    [
                        "manage-labels",
                        "--token",
                        "token",
                        "--org",
                        "leanprover-community",
                        "--response-file",
                        str(response_file),
                        "--dry-run",
                        "true",
                        "--github-output",
                        str(output_file),
                    ]
                )

            self.assertEqual(rc, 0)
            outputs = _parse_github_output(output_file)
            summary = outputs.get("label_summary", "")
            self.assertIn("Dry-run", summary)
            self.assertIn("Removed `doc-gen` label from runner `hoskinson1`", summary)

    def test_manage_labels_single_pass_keeps_standby_labels(self) -> None:
        """The once-per-run step must leave the standby labels as they are.

        Scenario:
        - real label-management execution path, nothing pending.
        - the default thresholds and one single check.

        Expected behavior:
        - no removal appears in the summary; the grace period is reported.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_file = tmp_path / "runners_response.json"
            output_file = tmp_path / "github_output.txt"
            _write_payload(response_file)

            with patch(
                "monitor_runners.label_management.PendingLabeledJobsClient.pending_labels",
                return_value=PendingLabeledJobs(pending=frozenset(), check_failed=False),
            ):
                rc = main(
                    [
                        "manage-labels",
                        "--token",
                        "token",
                        "--org",
                        "leanprover-community",
                        "--response-file",
                        str(response_file),
                        "--dry-run",
                        "true",
                        "--github-output",
                        str(output_file),
                    ]
                )

            self.assertEqual(rc, 0)
            outputs = _parse_github_output(output_file)
            summary = outputs.get("label_summary", "")
            self.assertNotIn("Removed", summary)
            self.assertIn("grace period", summary)


class _FakeClock:
    """Deterministic clock where only sleep() advances time."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def utcnow(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def _payload_dict() -> dict:
    """Return the minimal runners payload dict used by label-loop tests."""
    return {
        "total_count": 1,
        "runners": [
            {
                "id": 1,
                "name": "hoskinson1",
                "status": "online",
                "busy": False,
                "os": "Linux",
                "labels": [{"name": "pr", "type": "custom"}],
            }
        ],
    }


class WorkflowLabelLoopTests(unittest.TestCase):
    """Tests for workflow.py `label-loop` command iteration behavior."""

    def test_label_loop_repeats_until_deadline(self) -> None:
        """Loop should run one label-management pass per interval until the deadline.

        Scenario:
        - fake clock starts at t=0 with `--max-seconds 60` and `--interval-seconds 30`.
        - only sleep() advances the clock, so iterations land at t=0, 30, 60.

        Expected behavior:
        - exactly 3 iterations run, with a 30-second sleep between them.
        - each iteration passes the parsed dry_run flag to label management.
        - command exits 0.
        """
        clock = _FakeClock()
        with (
            patch("monitor_runners.workflow._utc_now", side_effect=clock.utcnow),
            patch("monitor_runners.workflow.time") as mock_time,
            patch(
                "monitor_runners.workflow._fetch_github_runners",
                return_value=_payload_dict(),
            ) as fetch,
            patch(
                "monitor_runners.workflow.execute_label_management",
                return_value=LabelManagementResult(
                    pending_labels="none",
                    busy_labels="none",
                    label_summary="",
                    label_errors="",
                ),
            ) as execute,
        ):
            mock_time.sleep.side_effect = clock.sleep
            rc = main(
                [
                    "label-loop",
                    "--token",
                    "token",
                    "--org",
                    "leanprover-community",
                    "--dry-run",
                    "true",
                    "--interval-seconds",
                    "30",
                    "--max-seconds",
                    "60",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(clock.sleeps, [30.0, 30.0])
        self.assertIs(execute.call_args.kwargs.get("dry_run"), True)

        # All iterations share one hysteresis, so both delays continue across
        # iterations of the same loop process.
        instances = [call.kwargs.get("hysteresis") for call in execute.call_args_list]
        self.assertIsInstance(instances[0], StandbyLabelHysteresis)
        self.assertEqual(instances[0].add_threshold, ADD_ITERATIONS)
        self.assertEqual(instances[0].keep_threshold, KEEP_ITERATIONS)
        self.assertTrue(all(instance is instances[0] for instance in instances))

    def test_label_loop_skips_iteration_on_fetch_failure(self) -> None:
        """A failed payload fetch should skip label management but keep looping.

        Scenario:
        - first fetch returns None (API failure), second returns a valid payload.
        - deadline allows exactly 2 iterations.

        Expected behavior:
        - label management runs only for the successful fetch.
        - command still exits 0.
        """
        clock = _FakeClock()
        with (
            patch("monitor_runners.workflow._utc_now", side_effect=clock.utcnow),
            patch("monitor_runners.workflow.time") as mock_time,
            patch(
                "monitor_runners.workflow._fetch_github_runners",
                side_effect=[None, _payload_dict()],
            ) as fetch,
            patch(
                "monitor_runners.workflow.execute_label_management",
                return_value=LabelManagementResult(
                    pending_labels="none",
                    busy_labels="none",
                    label_summary="",
                    label_errors="",
                ),
            ) as execute,
        ):
            mock_time.sleep.side_effect = clock.sleep
            rc = main(
                [
                    "label-loop",
                    "--token",
                    "token",
                    "--org",
                    "leanprover-community",
                    "--interval-seconds",
                    "30",
                    "--max-seconds",
                    "30",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(execute.call_count, 1)

    def test_label_loop_exits_cleanly_on_interrupt(self) -> None:
        """Cancellation (SIGINT/KeyboardInterrupt) should end the loop with exit 0.

        Scenario:
        - sleep raises KeyboardInterrupt, as when a newer run cancels this one.

        Expected behavior:
        - command returns 0 instead of propagating the interrupt.
        """
        clock = _FakeClock()
        with (
            patch("monitor_runners.workflow._utc_now", side_effect=clock.utcnow),
            patch("monitor_runners.workflow.time") as mock_time,
            patch(
                "monitor_runners.workflow._fetch_github_runners",
                return_value=_payload_dict(),
            ),
            patch(
                "monitor_runners.workflow.execute_label_management",
                return_value=LabelManagementResult(
                    pending_labels="none",
                    busy_labels="none",
                    label_summary="",
                    label_errors="",
                ),
            ),
        ):
            mock_time.sleep.side_effect = KeyboardInterrupt
            rc = main(
                [
                    "label-loop",
                    "--token",
                    "token",
                    "--org",
                    "leanprover-community",
                    "--interval-seconds",
                    "30",
                    "--max-seconds",
                    "3600",
                ]
            )

        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
