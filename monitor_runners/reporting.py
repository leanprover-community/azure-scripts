"""Weekly reporting helpers derived from host stats."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Tuple

from .constants import CANONICAL_HOSTS
from .models import MonitorStats, SampleState
from .time_utils import parse_timestamp


def render_weekly_report(stats: MonitorStats, generated_at: datetime) -> str:
    """Render the weekly Markdown summary from host stats."""
    stats_in = MonitorStats.from_dict(stats.to_dict())
    generated_s = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "**Weekly Runner Statistics Report**",
        "",
        f"*Period: Last 7 days - Generated: {generated_s}*",
        "",
        "| Runner | Idle | Active | Offline | Labels |",
        "|--------|------|---------|---------|--------|",
    ]

    for host in CANONICAL_HOSTS:
        runner = stats_in.runners[host]
        history = runner.history
        total = len(history)
        if total == 0:
            idle_pct = active_pct = offline_pct = 0.0
        else:
            idle = sum(1 for item in history if item.state == SampleState.IDLE)
            active = sum(1 for item in history if item.state == SampleState.ACTIVE)
            offline = sum(1 for item in history if item.state == SampleState.OFFLINE)
            idle_pct = round(idle * 100.0 / total, 2)
            active_pct = round(active * 100.0 / total, 2)
            offline_pct = round(offline * 100.0 / total, 2)

        labels_display = "-" if not runner.labels else f"`{runner.labels}`"
        lines.append(
            f"| `{host}` | {idle_pct}% | {active_pct}% | {offline_pct}% | {labels_display} |"
        )

    per_runner_state: Dict[str, List[Tuple[datetime, SampleState]]] = {}
    all_timestamps = set()
    for host in CANONICAL_HOSTS:
        entries: List[Tuple[datetime, SampleState]] = []
        for item in stats_in.runners[host].history:
            ts = parse_timestamp(item.timestamp)
            entries.append((ts, item.state))
            all_timestamps.add(ts)
        entries.sort(key=lambda pair: pair[0])
        per_runner_state[host] = entries

    sorted_timestamps = sorted(all_timestamps)
    if not sorted_timestamps:
        all_idle_pct = 0.0
        all_busy_pct = 0.0
    else:
        all_idle_count = 0
        all_busy_count = 0
        for timestamp in sorted_timestamps:
            states = []
            for host in CANONICAL_HOSTS:
                latest_state = SampleState.OFFLINE
                # Use latest known state at or before each shared timestamp.
                for entry_ts, entry_state in per_runner_state[host]:
                    if entry_ts <= timestamp:
                        latest_state = entry_state
                    else:
                        break
                states.append(latest_state)
            if all(state == SampleState.IDLE for state in states):
                all_idle_count += 1
            if all(state == SampleState.ACTIVE for state in states):
                all_busy_count += 1
        total = len(sorted_timestamps)
        all_idle_pct = round(all_idle_count * 100.0 / total, 2)
        all_busy_pct = round(all_busy_count * 100.0 / total, 2)

    total_data_points = sum(len(stats_in.runners[host].history) for host in CANONICAL_HOSTS)
    lines.extend(
        [
            "",
            "**Overall Statistics:**",
            f"- **All runners idle**: {all_idle_pct}% of monitoring periods",
            f"- **All runners busy**: {all_busy_pct}% of monitoring periods",
            "",
            "**Legend:**",
            "- **Idle**: Runner online but not executing jobs",
            "- **Active**: Runner online and executing jobs",
            "- **Offline**: Runner not responding",
            "",
            f"*Statistics based on {total_data_points} data points collected every 15 minutes.*",
        ]
    )
    return "\n".join(lines)
