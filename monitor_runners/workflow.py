"""GitHub Actions entrypoints for runner monitoring and weekly reporting.

This module is a thin CLI layer around the core monitoring/reporting logic.
It is responsible for:
- reading/writing JSON files used by workflow cache/artifacts
- fetching the GitHub runners payload
- writing step outputs to GITHUB_OUTPUT
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .core import process_monitoring_run
from .constants import host_for_name
from .label_management import (
    BORS_ACTIVE_BATCHES_URL,
    LabelManagementResult,
    execute_label_management,
)
from .models import GitHubRunnersPayload, MonitorState, MonitorStats
from .reporting import render_weekly_report


def _utc_now() -> datetime:
    """Return current UTC time for deterministic timestamp handling."""
    return datetime.now(timezone.utc)


def _load_json_file(path: str) -> Dict[str, Any]:
    """Load JSON file content, returning {} when file is missing or empty."""
    file_path = Path(path)
    if not file_path.exists():
        return {}
    content = file_path.read_text().strip()
    if not content:
        return {}
    return json.loads(content)


def _write_json_file(path: str, data: Dict[str, Any]) -> None:
    """Write JSON with stable pretty formatting and trailing newline."""
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


def _to_bool(value: str) -> bool:
    """Parse common truthy strings used by workflow dispatch inputs."""
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _write_output_line(output_path: str, key: str, value: str) -> None:
    """Append one single-line key=value output for GitHub Actions."""
    with Path(output_path).open("a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def _write_output_multiline(output_path: str, key: str, value: str) -> None:
    """Append one multiline output using GitHub's <<EOF format."""
    with Path(output_path).open("a", encoding="utf-8") as f:
        f.write(f"{key}<<EOF\n")
        f.write(value)
        if value and not value.endswith("\n"):
            f.write("\n")
        f.write("EOF\n")


def _fetch_github_runners(org: str, token: str) -> Optional[Dict[str, Any]]:
    """Fetch org runner payload; return None on network/HTTP/shape failures."""
    url = f"https://api.github.com/orgs/{org}/actions/runners"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    runners = payload.get("runners")
    if not isinstance(runners, list):
        return None
    return payload


def _find_unidentified_runner_names(payload: Dict[str, Any]) -> list[str]:
    """Return runner names that do not match the canonical host naming scheme."""
    unknown: list[str] = []
    for runner in payload.get("runners", []):
        name = runner.get("name")
        if isinstance(name, str) and host_for_name(name) is None:
            unknown.append(name)
    return sorted(set(unknown))


def _write_processing_error_outputs(output_path: str, messages: list[str]) -> None:
    """Write standardized processing-error outputs for downstream notification step."""
    if messages:
        _write_output_line(output_path, "has_processing_errors", "true")
        _write_output_multiline(output_path, "processing_errors", "\n\n".join(messages))
    else:
        _write_output_line(output_path, "has_processing_errors", "false")
        _write_output_multiline(output_path, "processing_errors", "")


def _write_label_management_outputs(output_path: str, result: LabelManagementResult) -> None:
    """Write label-management outputs consumed by workflow notification steps."""
    _write_output_line(output_path, "bors_active", str(result.bors_active).lower())
    _write_output_multiline(output_path, "label_summary", result.label_summary)
    if result.label_errors:
        _write_output_multiline(output_path, "label_errors", result.label_errors)
        _write_output_line(output_path, "has_label_errors", "true")
    else:
        _write_output_line(output_path, "label_errors", "")
        _write_output_line(output_path, "has_label_errors", "false")


def _run_check_runners(args: argparse.Namespace) -> int:
    """Execute the monitor step and emit all required workflow outputs.

    Contract:
    - Always exits 0 so downstream steps can inspect outputs.
    - Writes monitor outputs (api_ok/should_notify/message/...) to GITHUB_OUTPUT.
    - Writes processing-error outputs for dedicated error notifications.
    """
    output_path = args.github_output
    schedule = args.schedule or ""
    is_weekly_report = schedule == "0 9 * * 1" or _to_bool(args.send_weekly_report)
    processing_errors: list[str] = []

    # Weekly report may be triggered either by cron or workflow_dispatch input.
    _write_output_line(output_path, "is_weekly_report", str(is_weekly_report).lower())

    # Retry once to avoid noisy failures from transient API errors.
    payload = _fetch_github_runners(args.org, args.token)
    if payload is None:
        time.sleep(5)
        payload = _fetch_github_runners(args.org, args.token)
    if payload is None:
        state = MonitorState.from_dict(_load_json_file(args.state_file))
        processing_errors.append(
            "Runner monitor error: failed to fetch a valid GitHub runners payload after retry."
        )
        _write_output_line(output_path, "api_ok", "false")
        _write_output_line(output_path, "should_notify", "false")
        _write_output_multiline(output_path, "message", "")
        _write_output_line(output_path, "should_edit", "false")
        _write_output_line(
            output_path, "last_message_id", state.last_notification.message_id
        )
        _write_output_line(output_path, "offline_set", "[]")
        _write_processing_error_outputs(output_path, processing_errors)
        return 0

    # Unknown host names are treated as processing errors but do not block
    # normal monitor execution for known hosts.
    unknown_names = _find_unidentified_runner_names(payload)
    if unknown_names:
        unknown_list = ", ".join(f"`{name}`" for name in unknown_names)
        processing_errors.append(
            "Runner monitor error: unidentified runner host names in GitHub API payload:\n"
            f"{unknown_list}"
        )

    _write_json_file(args.response_file, payload)

    # Load persisted state/stats snapshots from cache-restored files.
    state = MonitorState.from_dict(_load_json_file(args.state_file))
    stats = MonitorStats.from_dict(_load_json_file(args.stats_file))
    try:
        now = _utc_now()
        result = process_monitoring_run(
            GitHubRunnersPayload.from_dict(payload),
            previous_state=state,
            previous_stats=stats,
            now=now,
        )
    except Exception as exc:
        processing_errors.append(f"Runner monitor error: processing failed with `{exc}`.")
        _write_output_line(output_path, "api_ok", "false")
        _write_output_line(output_path, "should_notify", "false")
        _write_output_multiline(output_path, "message", "")
        _write_output_line(output_path, "should_edit", "false")
        _write_output_line(
            output_path, "last_message_id", state.last_notification.message_id
        )
        _write_output_line(output_path, "offline_set", "[]")
        _write_processing_error_outputs(output_path, processing_errors)
        return 0

    # Persist updated state/stats and publish outputs used by downstream steps.
    _write_output_line(output_path, "api_ok", "true")
    _write_json_file(args.state_file, result.state.to_dict())
    _write_json_file(args.stats_file, result.stats.to_dict())

    message = result.message.replace("{org}", args.org)
    _write_output_line(output_path, "should_notify", str(result.should_notify).lower())
    _write_output_multiline(output_path, "message", message)
    _write_output_line(output_path, "should_edit", str(result.should_edit).lower())
    _write_output_line(output_path, "last_message_id", result.last_message_id)
    _write_output_line(output_path, "offline_set", json.dumps(result.offline_set))
    _write_processing_error_outputs(output_path, processing_errors)
    return 0


def _run_weekly_report(args: argparse.Namespace) -> int:
    """Execute weekly-report step and write weekly_message output."""
    stats = MonitorStats.from_dict(_load_json_file(args.stats_file))
    report = render_weekly_report(stats, generated_at=_utc_now())
    _write_output_multiline(args.github_output, "weekly_message", report)
    return 0


def _run_manage_labels(args: argparse.Namespace) -> int:
    """Execute label-management step and write label outputs."""
    dry_run = _to_bool(args.dry_run)
    if dry_run:
        print("DRY RUN: skipping runner label mutations (non-master branch)")

    payload = GitHubRunnersPayload.from_dict(_load_json_file(args.response_file))
    result = execute_label_management(
        payload=payload,
        org=args.org,
        token=args.token,
        dry_run=dry_run,
        bors_api_url=args.bors_api_url,
    )
    print(f"Bors active: {str(result.bors_active).lower()}")
    _write_label_management_outputs(args.github_output, result)

    # Keep behavior aligned with the old shell step cleanup.
    Path(args.response_file).unlink(missing_ok=True)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for workflow subcommands."""
    parser = argparse.ArgumentParser(description="Runner monitor workflow helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-runners")
    check.add_argument("--token", required=True)
    check.add_argument("--org", required=True)
    check.add_argument("--state-file", required=True)
    check.add_argument("--stats-file", required=True)
    check.add_argument("--response-file", default="runners_response.json")
    check.add_argument("--schedule", default="")
    check.add_argument("--send-weekly-report", default="false")
    check.add_argument(
        "--github-output", default=os.environ.get("GITHUB_OUTPUT", "")
    )

    weekly = subparsers.add_parser("weekly-report")
    weekly.add_argument("--stats-file", required=True)
    weekly.add_argument(
        "--github-output", default=os.environ.get("GITHUB_OUTPUT", "")
    )

    manage = subparsers.add_parser("manage-labels")
    manage.add_argument("--token", required=True)
    manage.add_argument("--org", required=True)
    manage.add_argument("--response-file", default="runners_response.json")
    manage.add_argument("--dry-run", default="false")
    manage.add_argument("--bors-api-url", default=BORS_ACTIVE_BATCHES_URL)
    manage.add_argument(
        "--github-output", default=os.environ.get("GITHUB_OUTPUT", "")
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint dispatching to workflow subcommands."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.github_output:
        parser.error("missing --github-output and GITHUB_OUTPUT is unset")

    if args.command == "check-runners":
        return _run_check_runners(args)
    if args.command == "weekly-report":
        return _run_weekly_report(args)
    if args.command == "manage-labels":
        return _run_manage_labels(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
