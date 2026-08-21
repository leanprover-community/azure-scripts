"""Runner label-management logic used by workflow CLI entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.parse
import urllib.request

from .constants import host_for_name
from .models import GitHubRunner, GitHubRunnersPayload

# Repos whose CI queues jobs on runners with the `pr` label.
PR_JOBS_REPOS = ("mathlib4", "mathlib4-nightly-testing")

# Upper bound on per-run job lookups per cycle; existence checks short-circuit,
# so this only matters when nothing is pending.
MAX_RUNS_TO_INSPECT = 20


@dataclass
class LabelManagementResult:
    """Label-management outputs written to GITHUB_OUTPUT."""

    pr_jobs_pending: bool | None
    label_summary: str
    label_errors: str


class PendingPrJobsClient:
    """Checks org repos for queued workflow jobs that request the `pr` label."""

    def __init__(self, org: str, token: str, repos: tuple[str, ...] = PR_JOBS_REPOS) -> None:
        self.org = org
        self.token = token
        self.repos = repos

    def _get_json(self, url: str) -> dict | None:
        """Fetch one JSON API page; return None on any failure."""
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"token {self.token}",
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
        return payload

    def has_pending_pr_jobs(self) -> bool | None:
        """Return true when some queued job requests `pr`; None when the check fails.

        A workflow run can sit in `queued` or hold queued jobs while
        `in_progress` (e.g. a matrix job waiting for a runner), so both run
        states are inspected. Queued runs are inspected first: they are the
        strongest starvation signal.
        """
        check_failed = False
        run_refs: list[tuple[str, int]] = []
        for status in ("queued", "in_progress"):
            for repo in self.repos:
                url = (
                    f"https://api.github.com/repos/{self.org}/{repo}/actions/runs"
                    f"?status={status}&per_page={MAX_RUNS_TO_INSPECT}"
                )
                payload = self._get_json(url)
                runs = payload.get("workflow_runs") if payload else None
                if not isinstance(runs, list):
                    check_failed = True
                    continue
                for run in runs:
                    run_id = run.get("id") if isinstance(run, dict) else None
                    if isinstance(run_id, int):
                        run_refs.append((repo, run_id))

        if len(run_refs) > MAX_RUNS_TO_INSPECT:
            skipped = len(run_refs) - MAX_RUNS_TO_INSPECT
            print(f"INFO: pending-pr-jobs check inspecting {MAX_RUNS_TO_INSPECT} runs, skipping {skipped}")
            run_refs = run_refs[:MAX_RUNS_TO_INSPECT]

        for repo, run_id in run_refs:
            url = (
                f"https://api.github.com/repos/{self.org}/{repo}/actions/runs/{run_id}/jobs"
                f"?filter=latest&per_page=100"
            )
            payload = self._get_json(url)
            jobs = payload.get("jobs") if payload else None
            if not isinstance(jobs, list):
                check_failed = True
                continue
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                labels = job.get("labels")
                if job.get("status") == "queued" and isinstance(labels, list) and "pr" in labels:
                    return True

        if check_failed:
            return None
        return False


class RunnerLabelApi:
    """GitHub API wrapper for runner label mutations."""

    def __init__(self, org: str, token: str, dry_run: bool) -> None:
        self.org = org
        self.token = token
        self.dry_run = dry_run

    def add_label(self, runner_id: int, label: str) -> bool:
        """Add one custom label to a runner."""
        if self.dry_run:
            return True
        url = f"https://api.github.com/orgs/{self.org}/actions/runners/{runner_id}/labels"
        request = urllib.request.Request(
            url,
            data=json.dumps({"labels": [label]}).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and isinstance(payload.get("labels"), list)

    def remove_label(self, runner_id: int, label: str) -> bool:
        """Remove one custom label from a runner."""
        if self.dry_run:
            return True
        encoded_label = urllib.parse.quote(label, safe="")
        url = (
            f"https://api.github.com/orgs/{self.org}/actions/runners/{runner_id}/labels/{encoded_label}"
        )
        request = urllib.request.Request(
            url,
            method="DELETE",
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status in {200, 204}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return False


class RunnerLabelManager:
    """Applies bors/pr label policy to one runner snapshot.

    State machine
    -------------
    [Start]
       |
       v
    [Phase 1] Ensure all idle runners have `bors`
       |
       v
    [Phase 2] Ensure at least one idle runner lacks `pr`, so a runner is
       always available when a bors batch starts
       - if already true: no mutation
       - else remove `pr` from first idle runner with `pr`
       - if no idle runner exists: log and continue
       |
       v
    [Phase 3] Depending on `pr_jobs_pending`
       |
       +--> if TRUE: add `pr` to each idle runner that:
       |        - has `bors` label
       |        - lacks `pr` label
       |        - is not the runner reserved in Phase 2
       |
       +--> if FALSE: leave `pr` labels unchanged; each host's own label
       |    table stays the baseline (the smaller pr pool stands)
       |
       +--> if None (check failed): same as FALSE, plus one error line

    Notes
    -----
    - Snapshot-based: no internal cross-run state.
    - Busy runners are never mutated by this state machine. This is because runners are ephemeral and label changes won't matter in this case.
    - Best-effort mutations: collect errors and continue.
    - Only runners on monitored (hoskinson*) hosts are managed; any other
      runner in the payload is ignored entirely, so experimental runners are
      never relabeled.
    """

    def __init__(self, payload: GitHubRunnersPayload, api: RunnerLabelApi) -> None:
        self.api = api
        self.runners = [
            runner for runner in payload.runners if host_for_name(runner.name) is not None
        ]
        ignored = sorted(
            runner.name for runner in payload.runners if host_for_name(runner.name) is None
        )
        if ignored:
            ignored_list = ", ".join(f"`{name}`" for name in ignored)
            print(f"INFO: ignoring unmonitored runner(s) for label management: {ignored_list}")
        self._summary_lines: list[str] = []
        self._error_lines: list[str] = []

    @staticmethod
    def _custom_labels(runner: GitHubRunner) -> set[str]:
        """Return custom label names for one runner."""
        return {label.name for label in runner.labels if label.type == "custom" and label.name}

    def _add_summary(self, line: str) -> None:
        """Append one human-readable summary line."""
        self._summary_lines.append(line)

    def _add_error(self, line: str) -> None:
        """Append one human-readable error line."""
        self._error_lines.append(line)

    @staticmethod
    def _is_idle(runner: GitHubRunner) -> bool:
        """Return true when runner is not busy, regardless of status."""
        return not runner.busy

    def _add_label(self, runner: GitHubRunner, label: str) -> None:
        """Add label and record either success or error output text."""
        ok = self.api.add_label(runner.runner_id, label)
        if ok:
            self._add_summary(f"Added `{label}` label to runner `{runner.name}`")
            return
        self._add_error(f"Failed to add `{label}` label to runner `{runner.name}`")

    def _remove_label(self, runner: GitHubRunner, label: str) -> None:
        """Remove label and record either success or error output text."""
        ok = self.api.remove_label(runner.runner_id, label)
        if ok:
            self._add_summary(f"Removed `{label}` label from runner `{runner.name}`")
            return
        self._add_error(f"Failed to remove `{label}` label from runner `{runner.name}`")

    def _ensure_bors_labels(self) -> None:
        """Ensure every idle runner includes the `bors` custom label."""
        for runner in self.runners:
            if runner.busy:
                continue
            if "bors" not in self._custom_labels(runner):
                self._add_label(runner, "bors")

    def _select_idle_runner_for_pr_removal(self) -> GitHubRunner | None:
        """Pick first idle runner that currently has custom label `pr`."""
        for runner in self.runners:
            if self._is_idle(runner) and "pr" in self._custom_labels(runner):
                return runner
        return None

    def _first_idle_runner_without_pr(self) -> GitHubRunner | None:
        """Return the first idle runner lacking `pr`: the reserved bors runner."""
        for runner in self.runners:
            if self._is_idle(runner) and "pr" not in self._custom_labels(runner):
                return runner
        return None

    def _ensure_reserved_bors_runner(self) -> None:
        """Keep at least one idle runner without `pr` so bors batches always start."""
        reserved = self._first_idle_runner_without_pr()
        if reserved is not None:
            self._add_summary(
                f"Runner `{reserved.name}` already lacks `pr` label (reserved for bors)"
            )
            return

        selected_runner = self._select_idle_runner_for_pr_removal()
        if selected_runner is None:
            print("INFO: No idle runners available to remove `pr` label from")
            return
        self._remove_label(selected_runner, "pr")

    def _add_pr_labels_for_pending_jobs(self) -> None:
        """Add missing `pr` to idle runners with `bors` so pending jobs get picked up.

        The reserved bors runner keeps lacking `pr`. When the reservation
        removed `pr` instead, the snapshot still shows that runner with `pr`,
        so it is not a candidate either way.
        """
        reserved = self._first_idle_runner_without_pr()
        candidates: list[GitHubRunner] = []
        for runner in self.runners:
            custom_labels = self._custom_labels(runner)
            if runner.busy or runner is reserved:
                continue
            if "bors" in custom_labels and "pr" not in custom_labels:
                candidates.append(runner)

        if not candidates:
            self._add_summary("Pending `pr` jobs; no `pr` labels to add")
            return

        for runner in candidates:
            self._add_label(runner, "pr")

    def apply_policy(self, pr_jobs_pending: bool | None) -> LabelManagementResult:
        """Execute label-management policy and return summarized outputs."""
        if not self.runners:
            self._add_error("**Label Management Error:** No monitored runners found in organization")
            return LabelManagementResult(
                pr_jobs_pending=pr_jobs_pending,
                label_summary="\n".join(self._summary_lines),
                label_errors="\n".join(self._error_lines),
            )

        # Keep `bors` present on idle runners before evaluating `pr` balancing.
        self._ensure_bors_labels()
        self._ensure_reserved_bors_runner()
        if pr_jobs_pending:
            self._add_pr_labels_for_pending_jobs()
        elif pr_jobs_pending is None:
            self._add_error(
                "**Label Management Error:** could not determine pending `pr` jobs; "
                "left `pr` labels unchanged"
            )
        else:
            self._add_summary("No pending `pr` jobs; left `pr` labels unchanged")

        return LabelManagementResult(
            pr_jobs_pending=pr_jobs_pending,
            label_summary="\n".join(self._summary_lines),
            label_errors="\n".join(self._error_lines),
        )


def execute_label_management(
    payload: GitHubRunnersPayload,
    org: str,
    token: str,
    dry_run: bool,
    pr_jobs_repos: tuple[str, ...] = PR_JOBS_REPOS,
) -> LabelManagementResult:
    """Run queue-aware label management for one runners payload."""
    pr_jobs_pending = PendingPrJobsClient(
        org=org, token=token, repos=pr_jobs_repos
    ).has_pending_pr_jobs()
    api = RunnerLabelApi(org=org, token=token, dry_run=dry_run)
    manager = RunnerLabelManager(payload=payload, api=api)
    result = manager.apply_policy(pr_jobs_pending=pr_jobs_pending)
    if dry_run:
        if result.label_summary:
            label_summary = f"Dry-run summary (these actions were not taken):\n{result.label_summary}"
        else:
            label_summary = "Dry-run"
        return LabelManagementResult(
            pr_jobs_pending=result.pr_jobs_pending,
            label_summary=label_summary,
            label_errors=result.label_errors,
        )
    return result
