from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure package imports work when tests are discovered as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor_runners import MonitorState, MonitorStats
from monitor_runners.workflow import main


def _parse_github_output(path: Path) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file with single-line and <<EOF multiline entries."""
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


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


class WorkflowErrorNotificationTests(unittest.TestCase):
    """Unit tests for workflow-level processing error outputs."""

    def test_unknown_host_sets_processing_error_output(self) -> None:
        """Unknown API runner names should produce a processing-error notification.

        Scenario:
        - check-runners receives a payload containing one known host and one
          unidentified host name.
        - state/stats files are present and deserializable.

        Expected behavior:
        - command exits successfully (returns 0) but sets:
          - has_processing_errors=true
          - processing_errors includes the unidentified host name
        - normal monitor outputs (api_ok/should_notify/etc.) are still emitted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_file = tmp_path / "runner-state.json"
            stats_file = tmp_path / "runner-stats.json"
            output_file = tmp_path / "github_output.txt"
            response_file = tmp_path / "runners_response.json"

            _write_json(state_file, MonitorState.from_dict({}).to_dict())
            _write_json(stats_file, MonitorStats.from_dict({}).to_dict())

            payload = {
                "total_count": 2,
                "runners": [
                    {
                        "id": 1,
                        "name": "hoskinson1",
                        "status": "online",
                        "busy": False,
                        "os": "Linux",
                        "labels": [{"name": "self-hosted", "type": "custom"}],
                    },
                    {
                        "id": 2,
                        "name": "mystery-runner-42",
                        "status": "online",
                        "busy": False,
                        "os": "Linux",
                        "labels": [{"name": "self-hosted", "type": "custom"}],
                    },
                ],
            }

            with patch("monitor_runners.workflow._fetch_github_runners", return_value=payload):
                rc = main(
                    [
                        "check-runners",
                        "--token",
                        "t",
                        "--org",
                        "leanprover-community",
                        "--state-file",
                        str(state_file),
                        "--stats-file",
                        str(stats_file),
                        "--response-file",
                        str(response_file),
                        "--github-output",
                        str(output_file),
                    ]
                )

            self.assertEqual(rc, 0)
            outputs = _parse_github_output(output_file)
            self.assertEqual(outputs.get("has_processing_errors"), "true")
            self.assertIn("mystery-runner-42", outputs.get("processing_errors", ""))
            self.assertEqual(outputs.get("api_ok"), "true")

    def test_processing_exception_sets_processing_error_output(self) -> None:
        """Unhandled processing exceptions should produce a processing-error output.

        Scenario:
        - API fetch succeeds with a valid payload.
        - process_monitoring_run raises an exception.

        Expected behavior:
        - command exits successfully (returns 0) to allow downstream notification.
        - has_processing_errors=true and error text is recorded.
        - monitor notification outputs are forced to no-op for this run.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_file = tmp_path / "runner-state.json"
            stats_file = tmp_path / "runner-stats.json"
            output_file = tmp_path / "github_output.txt"
            response_file = tmp_path / "runners_response.json"

            base_state = MonitorState.from_dict({})
            base_state.last_notification.message_id = "555"
            _write_json(state_file, base_state.to_dict())
            _write_json(stats_file, MonitorStats.from_dict({}).to_dict())

            payload = {
                "total_count": 1,
                "runners": [
                    {
                        "id": 1,
                        "name": "hoskinson1",
                        "status": "online",
                        "busy": False,
                        "os": "Linux",
                        "labels": [{"name": "self-hosted", "type": "custom"}],
                    }
                ],
            }

            with patch("monitor_runners.workflow._fetch_github_runners", return_value=payload):
                with patch(
                    "monitor_runners.workflow.process_monitoring_run",
                    side_effect=RuntimeError("boom"),
                ):
                    rc = main(
                        [
                            "check-runners",
                            "--token",
                            "t",
                            "--org",
                            "leanprover-community",
                            "--state-file",
                            str(state_file),
                            "--stats-file",
                            str(stats_file),
                            "--response-file",
                            str(response_file),
                            "--github-output",
                            str(output_file),
                        ]
                    )

            self.assertEqual(rc, 0)
            outputs = _parse_github_output(output_file)
            self.assertEqual(outputs.get("has_processing_errors"), "true")
            self.assertIn("processing failed", outputs.get("processing_errors", ""))
            self.assertEqual(outputs.get("api_ok"), "false")
            self.assertEqual(outputs.get("should_notify"), "false")
            self.assertEqual(outputs.get("last_message_id"), "555")


if __name__ == "__main__":
    unittest.main()
