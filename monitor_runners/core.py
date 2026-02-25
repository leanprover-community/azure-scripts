"""
Core run-processing logic for state/stats updates and notifications.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .constants import CANONICAL_HOSTS, host_for_name
from .models import (
    GitHubRunnersPayload,
    HistoryEntry,
    LastNotification,
    MonitorRunResult,
    MonitorState,
    MonitorStats,
    RunnerState,
    RunnerStatus,
    RunnerStats,
    SampleState,
)
from .time_utils import format_timestamp, parse_timestamp


@dataclass(frozen=True)
class HostObservation:
    """Aggregated one-run observation for a host.

    Attributes:
        present: True if at least one runner instance for this host
            appears in the current GitHub API payload.
        online: True if any present instance reports GitHub status ``online``.
        busy: True if any present+online instance reports ``busy=true``.
        labels: Label names selected for this host snapshot (host
            labels when present, otherwise merged labels from ephemeral instances).
        latest_runner_name: Most recently observed full runner name from
            this payload for the host (before host normalization), or empty
            string if host is missing.
    """

    present: bool
    online: bool
    busy: bool
    labels: List[str]
    latest_runner_name: str = ""


@dataclass(frozen=True)
class HostTransition:
    """One host transition result for a monitoring run.

    Attributes:
        host: Host name (for example ``hoskinson4``).
        new_state: Next persisted runner state for this host.
        sample_state: Stats sample state written for this run
            (``Idle``, ``Active``, or ``Offline``).
        became_newly_present: True when host reappears after being absent
            (previous consecutive_missing >= 2, now present).
        became_absent: True only on the transition edge where a missing host
            crosses into ``absent`` (second consecutive missing run).
        persistent_offline_checks: Consecutive present+offline count when it
            reaches alerting threshold (>=2), otherwise None.
        back_online_checks: Previous consecutive offline count when a host
            recovers online from an alerting offline streak, otherwise None.
    """

    host: str
    new_state: RunnerState
    sample_state: SampleState
    became_newly_present: bool = False
    became_absent: bool = False
    persistent_offline_checks: Optional[int] = None
    back_online_checks: Optional[int] = None


@dataclass(frozen=True)
class AlertPlan:
    """Notification decision output for a monitoring run.

    Attributes:
        should_notify: True when a non-empty status message should be sent.
        message: Markdown content to post/edit in Zulip.
        offline_set: Stable dedupe set of hosts currently considered problematic
            for notification state tracking (absent or persistent offline).
        should_edit: True when the workflow should edit the previous Zulip
            message instead of posting a new one.
        last_message_id: Prior message id from persisted notification state;
            empty when no previous message is tracked.
    """

    should_notify: bool
    message: str
    offline_set: List[str]
    should_edit: bool
    last_message_id: str


def _dedupe_labels(items: Iterable[str]) -> List[str]:
    """Return items in original order with duplicates removed."""
    seen = set()
    ordered = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _labels_string(labels: Sequence[str]) -> str:
    """Serialize label names into stable comma-separated text."""
    return ",".join(_dedupe_labels(labels))


def _format_section(title: str, lines: Sequence[str]) -> str:
    """Format a Markdown section with heading and bullet lines."""
    section_lines = [title, ""]
    section_lines.extend(lines)
    return "\n".join(section_lines)


def _format_label_text(labels_csv: str) -> str:
    """Format label text used in human-readable notifications."""
    if labels_csv:
        return f"labels: `{labels_csv}`"
    return "no labels"


def _format_last_seen_runner_name_text(full_name: str) -> str:
    """Format last-seen runner name text for absent-host notifications."""
    if full_name:
        return f"last seen as `{full_name}`"
    return "last full runner name unknown"


def _format_current_runner_name_text(full_name: str) -> str:
    """Format current runner name text for newly-present notifications."""
    if full_name:
        return f"currently seen as `{full_name}`"
    return "current full runner name unknown"


def _runner_state(online: bool, busy: bool) -> SampleState:
    """Convert online/busy booleans into Idle/Active/Offline state."""
    if not online:
        return SampleState.OFFLINE
    if busy:
        return SampleState.ACTIVE
    return SampleState.IDLE


def _select_latest_runner_name(entries: Sequence[Dict[str, Any]]) -> str:
    """Pick the lexicographically largest full runner name for one host."""
    return max(str(entry["name"]) for entry in entries)


def _aggregate_payload(payload: GitHubRunnersPayload) -> Dict[str, HostObservation]:
    """Aggregate API runner objects into one observation per host."""
    grouped: Dict[str, List[Dict[str, Any]]] = {host: [] for host in CANONICAL_HOSTS}
    for runner in payload.runners:
        name = runner.name
        if not name:
            continue
        host = host_for_name(name)
        if host is None:
            continue
        grouped[host].append(
            {
                "name": name,
                "status": runner.status,
                "busy": runner.busy,
                "labels": runner.label_names(),
            }
        )

    aggregated: Dict[str, HostObservation] = {}
    for host in CANONICAL_HOSTS:
        entries = grouped[host]
        if not entries:
            aggregated[host] = HostObservation(
                present=False,
                online=False,
                busy=False,
                labels=[],
                latest_runner_name="",
            )
            continue

        # A host is online if any instance reports online.
        online = any(entry["status"] == "online" for entry in entries)
        # A host is busy if any online instance is busy.
        busy = any(entry["status"] == "online" and entry["busy"] for entry in entries)
        entry = next((entry for entry in entries if entry["name"] == host), None)
        if entry is not None:
            labels = entry["labels"]
        else:
            # If only ephemeral instances exist, merge their labels as fallback.
            merged: List[str] = []
            for entry in entries:
                merged.extend(entry["labels"])
            labels = _dedupe_labels(merged)

        aggregated[host] = HostObservation(
            present=True,
            online=online,
            busy=busy,
            labels=labels,
            latest_runner_name=_select_latest_runner_name(entries),
        )
    return aggregated


class HostStateMachine:
    """    
    State-machine overview
    ======================

    The monitor tracks each host with two coupled machines:

    1) Presence machine (is the host present in the GitHub API payload?)

        PRESENT
        | missing once
        v
        MISSING_1 (status=offline, consecutive_missing=1)
        | missing again
        v
        ABSENT (status=absent, consecutive_missing>=2, entry event on transition)
        | still missing
        v
        ABSENT

        Recovery:
        - MISSING_1 -> PRESENT: no alert
        - ABSENT -> PRESENT: emit "newly present" alert and reset consecutive_missing

    2) Availability machine (only evaluated when host is present)

        ONLINE
        | present+offline
        v
        OFFLINE_1 (consecutive_offline=1, no alert)
        | present+offline
        v
        OFFLINE_N (N>=2, emit persistent-offline alert)
        | present+online
        v
        ONLINE (emit back-online alert if coming from OFFLINE_N where N>=2)

        Stats samples use {Idle, Active, Offline}; missing/absent hosts are sampled as Offline.
    """

    def apply(
        self, host: str, previous: RunnerState, observation: HostObservation
    ) -> HostTransition:
        """Compute the next state and transition events for one host."""
        prev_status = previous.status
        prev_consecutive_offline = previous.consecutive_offline
        prev_consecutive_missing = previous.consecutive_missing
        prev_labels = previous.labels
        prev_last_known_runner_name = previous.last_known_runner_name

        if observation.present:
            labels_csv = _labels_string(observation.labels) or prev_labels
            became_newly_present = prev_consecutive_missing >= 2
            last_known_runner_name = (
                observation.latest_runner_name or prev_last_known_runner_name
            )

            if observation.online:
                status = RunnerStatus.ONLINE
                consecutive_offline = 0
                back_online_checks = (
                    prev_consecutive_offline
                    if prev_status == RunnerStatus.OFFLINE and prev_consecutive_offline >= 2
                    else None
                )
                persistent_offline_checks = None
            else:
                status = RunnerStatus.OFFLINE
                consecutive_offline = (
                    prev_consecutive_offline + 1
                    if prev_status == RunnerStatus.OFFLINE
                    else 1
                )
                persistent_offline_checks = (
                    consecutive_offline if consecutive_offline >= 2 else None
                )
                back_online_checks = None

            next_state = RunnerState(
                status=status,
                consecutive_offline=consecutive_offline,
                consecutive_missing=0,
                labels=labels_csv,
                last_known_runner_name=last_known_runner_name,
            )
            sample_state = _runner_state(online=observation.online, busy=observation.busy)
            return HostTransition(
                host=host,
                new_state=next_state,
                sample_state=sample_state,
                became_newly_present=became_newly_present,
                became_absent=False,
                persistent_offline_checks=persistent_offline_checks,
                back_online_checks=back_online_checks,
            )

        consecutive_missing = prev_consecutive_missing + 1
        became_absent = consecutive_missing >= 2 and prev_consecutive_missing < 2
        status = RunnerStatus.ABSENT if consecutive_missing >= 2 else RunnerStatus.OFFLINE
        next_state = RunnerState(
            status=status,
            consecutive_offline=0,
            consecutive_missing=consecutive_missing,
            labels=prev_labels,
            last_known_runner_name=prev_last_known_runner_name,
        )
        return HostTransition(
            host=host,
            new_state=next_state,
            sample_state=SampleState.OFFLINE,
            became_newly_present=False,
            became_absent=became_absent,
            persistent_offline_checks=None,
            back_online_checks=None,
        )


class AlertPlanner:
    """Builds notification message and edit policy from host transitions."""

    @classmethod
    def build(
        self, transitions: Sequence[HostTransition], last_notification: LastNotification
    ) -> AlertPlan:
        """Compute alert content and dedupe/edit metadata."""
        # Group transitions by alert-relevant event type.
        back_online = [t for t in transitions if t.back_online_checks is not None]
        newly_present = [t for t in transitions if t.became_newly_present]
        absent_entries = [t for t in transitions if t.became_absent]
        absent_for_multiple = [
            t
            for t in transitions
            if t.new_state.status == RunnerStatus.ABSENT
            and t.new_state.consecutive_missing >= 2
        ]
        persistent_offline = [
            t for t in transitions if t.persistent_offline_checks is not None
        ]

        # offline_set drives dedupe/edit behavior across runs.
        # It includes hosts that are either absent or persistently offline.
        offline_hosts = [
            t.host
            for t in transitions
            if t.new_state.status == RunnerStatus.ABSENT
            or t.persistent_offline_checks is not None
        ]
        offline_set = sorted(set(offline_hosts))

        sections: List[str] = []
        if back_online:
            lines = []
            for transition in back_online:
                labels = transition.new_state.labels
                checks = transition.back_online_checks
                lines.append(
                    f"- `{transition.host}` (was offline for {checks} checks, {_format_label_text(labels)})"
                )
            sections.append(
                _format_section(
                    "**[Runners](https://github.com/organizations/{org}/settings/actions/runners) back online:**",
                    lines,
                )
            )

        if newly_present:
            lines = []
            for transition in newly_present:
                labels = transition.new_state.labels
                full_name_text = _format_current_runner_name_text(
                    transition.new_state.last_known_runner_name
                )
                lines.append(
                    f"- `{transition.host}` ({full_name_text}, {_format_label_text(labels)})"
                )
            sections.append(
                _format_section(
                    "**Runners newly present in API payload:**",
                    lines,
                )
            )

        if absent_for_multiple:
            lines = []
            for transition in absent_for_multiple:
                labels = transition.new_state.labels
                checks = transition.new_state.consecutive_missing
                full_name_text = _format_last_seen_runner_name_text(
                    transition.new_state.last_known_runner_name
                )
                lines.append(
                    f"- `{transition.host}` ({checks} consecutive missing checks, {full_name_text}, {_format_label_text(labels)})"
                )
            sections.append(
                _format_section(
                    "**Runners absent from API payload for multiple checks:**",
                    lines,
                )
            )

        if persistent_offline:
            lines = []
            for transition in persistent_offline:
                labels = transition.new_state.labels
                checks = transition.persistent_offline_checks
                lines.append(
                    f"- `{transition.host}` ({checks} consecutive checks, {_format_label_text(labels)})"
                )
            sections.append(
                _format_section(
                    "**[Runners](https://github.com/organizations/{org}/settings/actions/runners) offline for multiple checks:**",
                    lines,
                )
            )

        message = "\n\n".join(sections)
        should_notify = bool(message)
        last_message_id = last_notification.message_id
        last_offline_set = sorted(last_notification.offline_set)
        
        # Edit-in-place is for steady-state "still problematic" updates
        # (persistent offline and/or persistent absent).
        # Any recovery/new-absence-entry/new-presence event forces a fresh message.
        should_edit = (
            should_notify
            and bool(last_message_id)
            and offline_set == last_offline_set
            and not back_online
            and not newly_present
            and not absent_entries
        )

        return AlertPlan(
            should_notify=should_notify,
            message=message,
            offline_set=offline_set,
            should_edit=should_edit,
            last_message_id=last_message_id,
        )


def process_monitoring_run(
    payload: GitHubRunnersPayload,
    previous_state: MonitorState,
    previous_stats: MonitorStats,
    now: datetime,
) -> MonitorRunResult:
    """Compute next state/stats and notifications for one monitoring run.

    High-level flow:
    1) Collapse raw GitHub runner payload into one observation per host.
    2) Apply per-host transition rules (presence + availability state machines).
    3) Update rolling 7-day stats with one fresh sample per host.
    4) Derive alert message + edit policy from transition events.
    """
    # Normalize "now" to UTC and canonical persisted timestamp format.
    now_utc = now.astimezone(timezone.utc)
    now_s = format_timestamp(now_utc)

    # Convert the payload (which may include multiple ephemeral instances per host)
    # into one host-level snapshot used by the state machine.
    aggregated = _aggregate_payload(payload)

    # Work on normalized copies so transition/stat logic sees complete host keys
    # and cannot mutate caller-provided objects.
    state_in = MonitorState.from_dict(previous_state.to_dict())
    stats_in = MonitorStats.from_dict(previous_stats.to_dict())

    # Start the next state from current timestamp and carry over last_notification.
    # last_notification is updated later in the workflow after a message is sent.
    new_state = MonitorState(
        last_run=now_s,
        runners={},
        last_notification=state_in.last_notification.clone(),
    )

    # Compute one transition per host and collect:
    # - new persisted state fields
    # - current sample state for stats (Idle/Active/Offline)
    # - transition events for alert planning
    state_machine = HostStateMachine()
    transitions: List[HostTransition] = []
    runner_state_for_stats: Dict[str, SampleState] = {}
    for host in CANONICAL_HOSTS:
        transition = state_machine.apply(host, state_in.runners[host], aggregated[host])
        transitions.append(transition)
        new_state.runners[host] = transition.new_state
        runner_state_for_stats[host] = transition.sample_state

    # Build next stats with a rolling 7-day retention window and one new sample
    # per host for this run.
    cutoff = now_utc - timedelta(days=7)
    new_stats = MonitorStats(runners={}, last_cleanup=now_s)
    for host in CANONICAL_HOSTS:
        previous_runner_stats = stats_in.runners[host]
        filtered_history: List[HistoryEntry] = []
        for entry in previous_runner_stats.history:
            # Keep only a rolling 7-day history window.
            if parse_timestamp(entry.timestamp) >= cutoff:
                filtered_history.append(entry)

        filtered_history.append(
            HistoryEntry(timestamp=now_s, state=runner_state_for_stats[host])
        )
        # Prefer current labels; if unavailable, keep the last known label set.
        labels_csv = new_state.runners[host].labels or previous_runner_stats.labels
        new_stats.runners[host] = RunnerStats(history=filtered_history, labels=labels_csv)

    # Convert transition events into user-facing alert content and dedupe metadata.
    alert_plan = AlertPlanner.build(transitions, state_in.last_notification)

    # Return typed outputs plus alert fields expected by workflow wiring.
    return MonitorRunResult(
        state=new_state,
        stats=new_stats,
        should_notify=alert_plan.should_notify,
        message=alert_plan.message,
        offline_set=alert_plan.offline_set,
        should_edit=alert_plan.should_edit,
        last_message_id=alert_plan.last_message_id,
    )
