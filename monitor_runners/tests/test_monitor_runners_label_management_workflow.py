from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure package imports work when tests are discovered as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from monitor_runners.label_management import LabelManagementResult
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


def _write_payload(path: Path) -> None:
    """Write the minimal runners payload file used by CLI integration tests."""
    payload = {
        "total_count": 1,
        "runners": [
            {
                "id": 1,
                "name": "alpha",
                "status": "online",
                "busy": False,
                "os": "Linux",
                "labels": [
                    {"name": "bors", "type": "custom"},
                    {"name": "pr", "type": "custom"},
                ],
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
        - outputs include bors_active/label_summary/label_errors/has_label_errors.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_file = tmp_path / "runners_response.json"
            output_file = tmp_path / "github_output.txt"
            _write_payload(response_file)

            with patch(
                "monitor_runners.workflow.execute_label_management",
                return_value=LabelManagementResult(
                    bors_active=False,
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
            self.assertEqual(outputs.get("bors_active"), "false")
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
                    bors_active=True,
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

    def test_manage_labels_dry_run_summary_has_clear_prefix(self) -> None:
        """Dry-run mode should prefix summary while keeping normal mutation wording.

        Scenario:
        - command runs with `--dry-run true`.
        - real label-management execution path is used.

        Expected behavior:
        - summary starts with "Would-be summary:".
        - summary still contains regular mutation wording ("Removed ...").
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_file = tmp_path / "runners_response.json"
            output_file = tmp_path / "github_output.txt"
            _write_payload(response_file)

            with patch(
                "monitor_runners.label_management.BorsStatusClient.has_active_batches",
                return_value=True,
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
            self.assertIn(
                "Dry-run summary (these actions were not taken):",
                outputs.get("label_summary", ""),
            )
            self.assertIn("Removed `pr` label from runner `alpha`", outputs.get("label_summary", ""))


if __name__ == "__main__":
    unittest.main()
