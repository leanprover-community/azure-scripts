"""Runner label-management logic used by workflow CLI entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.parse
import urllib.request

from .models import GitHubRunner, GitHubRunnersPayload

BORS_ACTIVE_BATCHES_URL = "https://mathlib-bors-ca18eefec4cb.herokuapp.com/api/active-batches"


@dataclass
class LabelManagementResult:
    """Label-management outputs written to GITHUB_OUTPUT."""

    bors_active: bool
    label_summary: str
    label_errors: str


class BorsStatusClient:
    """Queries bors active-batch status with conservative failure fallback."""

    def __init__(self, url: str) -> None:
        self.url = url

    def has_active_batches(self) -> bool:
        """Return true when bors has active batches; failures default to true."""
        request = urllib.request.Request(self.url)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            return True

        if not isinstance(payload, dict):
            return True
        batch_ids = payload.get("batch_ids")
        if isinstance(batch_ids, list):
            return len(batch_ids) > 0
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
    [Phase 2] Depending on `bors_active`
       |
       +--> if TRUE: ensure at least one idle runner lacks `pr`
       |    - if already true: no mutation
       |    - else remove `pr` from first idle runner with `pr`
       |    - if no idle runner exists: emit an error message for tracing
       |
       +--> if FALSE: add `pr` to each idle runner that:
                - has `bors` label
                - lacks `pr` label

    Notes
    -----
    - Snapshot-based: no internal cross-run state.
    - Busy runners are never mutated by this state machine. This is because runners are ephemeral and label changes won't matter in this case.
    - Best-effort mutations: collect errors and continue.
    """

    def __init__(self, payload: GitHubRunnersPayload, api: RunnerLabelApi) -> None:
        self.payload = payload
        self.api = api
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
        for runner in self.payload.runners:
            if runner.busy:
                continue
            if "bors" not in self._custom_labels(runner):
                self._add_label(runner, "bors")

    def _select_idle_runner_for_pr_removal(self) -> GitHubRunner | None:
        """Pick first idle runner that currently has custom label `pr`."""
        for runner in self.payload.runners:
            if self._is_idle(runner) and "pr" in self._custom_labels(runner):
                return runner
        return None

    def _manage_pr_labels_when_bors_active(self) -> None:
        """Keep one idle runner without `pr` while bors has active batches."""
        for runner in self.payload.runners:
            if self._is_idle(runner) and "pr" not in self._custom_labels(runner):
                self._add_summary(
                    f"Runner `{runner.name}` already lacks `pr` label (no changes needed)"
                )
                return

        selected_runner = self._select_idle_runner_for_pr_removal()
        if selected_runner is None:
            self._add_error(
                "**Label Management Error:** No idle runners available to remove `pr` label from"
            )
            return
        self._remove_label(selected_runner, "pr")

    def _manage_pr_labels_when_bors_inactive(self) -> None:
        """Restore missing `pr` only for idle runners that have `bors`."""
        runners_without_pr: list[GitHubRunner] = []
        for runner in self.payload.runners:
            custom_labels = self._custom_labels(runner)
            if runner.busy:
                continue
            if "bors" in custom_labels and "pr" not in custom_labels:
                runners_without_pr.append(runner)

        if not runners_without_pr:
            self._add_summary("No idle runners missing `pr` label")
            return

        for runner in runners_without_pr:
            self._add_label(runner, "pr")

    def apply_policy(self, bors_active: bool) -> LabelManagementResult:
        """Execute label-management policy and return summarized outputs."""
        if not self.payload.runners:
            self._add_error("**Label Management Error:** No runners found in organization")
            return LabelManagementResult(
                bors_active=bors_active,
                label_summary="\n".join(self._summary_lines),
                label_errors="\n".join(self._error_lines),
            )

        # Keep `bors` present on idle runners before evaluating `pr` balancing.
        self._ensure_bors_labels()
        if bors_active:
            self._manage_pr_labels_when_bors_active()
        else:
            self._manage_pr_labels_when_bors_inactive()

        return LabelManagementResult(
            bors_active=bors_active,
            label_summary="\n".join(self._summary_lines),
            label_errors="\n".join(self._error_lines),
        )


def execute_label_management(
    payload: GitHubRunnersPayload,
    org: str,
    token: str,
    dry_run: bool,
    bors_api_url: str = BORS_ACTIVE_BATCHES_URL,
) -> LabelManagementResult:
    """Run bors-aware label management for one runners payload."""
    bors_active = BorsStatusClient(bors_api_url).has_active_batches()
    api = RunnerLabelApi(org=org, token=token, dry_run=dry_run)
    manager = RunnerLabelManager(payload=payload, api=api)
    result = manager.apply_policy(bors_active=bors_active)
    if dry_run:
        if result.label_summary:
            label_summary = f"Dry-run summary (these actions were not taken):\n{result.label_summary}"
        else:
            label_summary = "Dry-run"
        return LabelManagementResult(
            bors_active=result.bors_active,
            label_summary=label_summary,
            label_errors=result.label_errors,
        )
    return result
