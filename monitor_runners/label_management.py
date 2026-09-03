"""Runner label-management logic used by workflow CLI entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.parse
import urllib.request

from .constants import host_for_name
from .models import GitHubRunner, GitHubRunnersPayload

# Repos whose CI queues jobs on runners with the standby labels.
LABELED_JOBS_REPOS = ("mathlib4", "mathlib4-nightly-testing")

# Labels the standby mechanism may add to idle hoskinson runners.
STANDBY_LABELS = ("pr", "bors")

# Labels the standby mechanism strips from idle hoskinson runners. Includes
# the specialty labels so runners registered under the old per-host label
# tables are cleaned up as well.
ROUTING_LABELS = ("pr", "bors", "doc-gen", "lean4checker")

# Runners carrying this label are being drained for disk cleanup
# (leanprover-community/docker, host-management/auto-cleanup.sh) and must not
# be relabeled.
CLEANUP_LABEL = "cleanup-soon"

# Upper bound on per-run job lookups per cycle; existence checks short-circuit,
# so this only matters when nothing is pending.
MAX_RUNS_TO_INSPECT = 20


@dataclass
class LabelManagementResult:
    """Label-management outputs written to GITHUB_OUTPUT."""

    pending_labels: str
    fleet_busy: bool
    label_summary: str
    label_errors: str


@dataclass
class PendingLabeledJobs:
    """Which standby labels have queued jobs, and whether the check was complete.

    Labels in `pending` are definitely requested by some queued job, even when
    `check_failed` is true. A failed check only makes *absence* uncertain: a
    label missing from `pending` may still have queued jobs.
    """

    pending: frozenset[str]
    check_failed: bool


class PendingLabeledJobsClient:
    """Checks org repos for queued workflow jobs that request standby labels."""

    def __init__(
        self,
        org: str,
        token: str,
        labels: tuple[str, ...] = STANDBY_LABELS,
        repos: tuple[str, ...] = LABELED_JOBS_REPOS,
    ) -> None:
        self.org = org
        self.token = token
        self.labels = labels
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

    def pending_labels(self) -> PendingLabeledJobs:
        """Return the standby labels requested by queued jobs.

        A workflow run can sit in `queued` or hold queued jobs while
        `in_progress` (e.g. a matrix job waiting for a runner), so both run
        states are inspected. Queued runs are inspected first: they are the
        strongest starvation signal.
        """
        wanted = set(self.labels)
        pending: set[str] = set()
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
            print(f"INFO: pending-jobs check inspecting {MAX_RUNS_TO_INSPECT} runs, skipping {skipped}")
            run_refs = run_refs[:MAX_RUNS_TO_INSPECT]

        for repo, run_id in run_refs:
            if pending == wanted:
                break
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
                if job.get("status") == "queued" and isinstance(labels, list):
                    pending.update(wanted.intersection(labels))

        return PendingLabeledJobs(pending=frozenset(pending), check_failed=check_failed)


def render_pending_labels(pending_jobs: PendingLabeledJobs) -> str:
    """Render the pending-labels result for outputs and logs."""
    labels = ",".join(sorted(pending_jobs.pending))
    if pending_jobs.check_failed:
        return f"{labels} (check incomplete)" if labels else "unknown"
    return labels or "none"


def fleet_is_busy(payload: GitHubRunnersPayload) -> bool:
    """Return true when every online non-hoskinson runner is busy.

    Vacuously true when no non-hoskinson runner is online: with no other
    capacity available, the hoskinson standby pool may take jobs.
    """
    for runner in payload.runners:
        if host_for_name(runner.name) is not None:
            continue
        if runner.status == "online" and not runner.busy:
            return False
    return True


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
    """Applies the standby label policy to one runner snapshot.

    The hoskinson hosts are standby capacity: their ephemeral runners register
    without routing labels and take jobs only when this policy hands out a
    label. The non-hoskinson fleet serves all queues by default.

    State machine
    -------------
    [Start]
       |
       v
    Compute `active` labels: a standby label is active when the whole
    non-hoskinson fleet is busy AND some queued job requests it.
       |
       v
    [Phase 1] Add each active label to every idle online hoskinson runner
       that lacks it, so the starved queue gets standby capacity.
       |
       v
    [Phase 2] Remove every non-active routing label from every idle
       hoskinson runner, returning the standby pool to its unlabeled
       baseline. Offline idle runners are stripped too.

    When the pending-jobs check is incomplete, labels found pending are still
    added (Phase 1); Phase 2 is skipped entirely, because a label missing from
    the pending set may still have queued jobs.

    Notes
    -----
    - Snapshot-based: no internal cross-run state.
    - Busy runners are never mutated. Runners are ephemeral: a busy runner
      deregisters after its job and its replacement registers unlabeled.
    - Runners carrying the `cleanup-soon` drain label are never mutated.
    - Best-effort mutations: collect errors and continue.
    - Only runners on monitored (hoskinson*) hosts are mutated; any other
      runner in the payload only feeds the fleet-busy signal.
    """

    def __init__(self, payload: GitHubRunnersPayload, api: RunnerLabelApi) -> None:
        self.api = api
        self.runners = [
            runner for runner in payload.runners if host_for_name(runner.name) is not None
        ]
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

    def _is_draining(self, runner: GitHubRunner) -> bool:
        """Return true when runner carries the cleanup drain label."""
        return CLEANUP_LABEL in self._custom_labels(runner)

    def _add_active_labels(self, active: set[str]) -> None:
        """Add each active label to idle online runners that lack it."""
        for label in STANDBY_LABELS:
            if label not in active:
                continue
            for runner in self.runners:
                if not self._is_idle(runner) or runner.status != "online":
                    continue
                if self._is_draining(runner):
                    continue
                if label not in self._custom_labels(runner):
                    self._add_label(runner, label)

    def _remove_inactive_labels(self, active: set[str]) -> None:
        """Strip non-active routing labels from idle runners."""
        for runner in self.runners:
            if not self._is_idle(runner) or self._is_draining(runner):
                continue
            custom_labels = self._custom_labels(runner)
            for label in ROUTING_LABELS:
                if label in custom_labels and label not in active:
                    self._remove_label(runner, label)

    def apply_policy(
        self, pending_jobs: PendingLabeledJobs, fleet_busy: bool
    ) -> LabelManagementResult:
        """Execute the standby label policy and return summarized outputs."""
        pending_text = render_pending_labels(pending_jobs)
        if not self.runners:
            self._add_error("**Label Management Error:** No monitored runners found in organization")
            return LabelManagementResult(
                pending_labels=pending_text,
                fleet_busy=fleet_busy,
                label_summary="\n".join(self._summary_lines),
                label_errors="\n".join(self._error_lines),
            )

        if fleet_busy:
            active = set(STANDBY_LABELS).intersection(pending_jobs.pending)
        else:
            active = set()

        if active:
            active_text = ", ".join(f"`{label}`" for label in sorted(active))
            self._add_summary(
                f"Fleet busy with pending jobs; standby labels active: {active_text}"
            )
        elif fleet_busy:
            self._add_summary("Fleet busy but no pending standby jobs; standby stays unlabeled")
        else:
            self._add_summary("Idle fleet capacity available; standby stays unlabeled")

        self._add_active_labels(active)
        if pending_jobs.check_failed:
            self._add_error(
                "**Label Management Error:** pending-jobs check incomplete; "
                "left non-active labels in place"
            )
        else:
            self._remove_inactive_labels(active)

        return LabelManagementResult(
            pending_labels=pending_text,
            fleet_busy=fleet_busy,
            label_summary="\n".join(self._summary_lines),
            label_errors="\n".join(self._error_lines),
        )


def execute_label_management(
    payload: GitHubRunnersPayload,
    org: str,
    token: str,
    dry_run: bool,
    labeled_jobs_repos: tuple[str, ...] = LABELED_JOBS_REPOS,
) -> LabelManagementResult:
    """Run queue-aware standby label management for one runners payload."""
    pending_jobs = PendingLabeledJobsClient(
        org=org, token=token, repos=labeled_jobs_repos
    ).pending_labels()
    api = RunnerLabelApi(org=org, token=token, dry_run=dry_run)
    manager = RunnerLabelManager(payload=payload, api=api)
    result = manager.apply_policy(pending_jobs=pending_jobs, fleet_busy=fleet_is_busy(payload))
    if dry_run:
        if result.label_summary:
            label_summary = f"Dry-run summary (these actions were not taken):\n{result.label_summary}"
        else:
            label_summary = "Dry-run"
        return LabelManagementResult(
            pending_labels=result.pending_labels,
            fleet_busy=result.fleet_busy,
            label_summary=label_summary,
            label_errors=result.label_errors,
        )
    return result
