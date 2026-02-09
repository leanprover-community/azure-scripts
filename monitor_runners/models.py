"""Structured models for persisted monitor data and GitHub API payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .constants import CANONICAL_HOSTS


class RunnerStatus(str, Enum):
    """Persisted runner status values used in runner-state.json."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    ABSENT = "absent"

    @classmethod
    def from_value(cls, value: Any) -> "RunnerStatus":
        """Parse a status value from JSON/string input with safe fallback."""
        if isinstance(value, cls):
            return value
        text = str(value or "").lower()
        for member in cls:
            if member.value == text:
                return member
        return cls.UNKNOWN


class SampleState(str, Enum):
    """Sample state values used in runner-stats.json history entries."""

    IDLE = "Idle"
    ACTIVE = "Active"
    OFFLINE = "Offline"

    @classmethod
    def from_value(cls, value: Any) -> Optional["SampleState"]:
        """Parse a sample state from JSON/string input, if recognized."""
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        lookup = {
            "idle": cls.IDLE,
            "active": cls.ACTIVE,
            "offline": cls.OFFLINE,
        }
        return lookup.get(text)


@dataclass
class GitHubLabel:
    """One runner label object from the GitHub Actions runners API."""

    name: str = ""
    type: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GitHubLabel":
        """Deserialize GitHubLabel from a plain dictionary."""
        source = data or {}
        return cls(name=str(source.get("name", "")), type=str(source.get("type", "")))

    def to_dict(self) -> Dict[str, str]:
        """Serialize GitHubLabel into a dictionary."""
        return {"name": self.name, "type": self.type}


@dataclass
class GitHubRunner:
    """One runner entry from the GitHub Actions runners API."""

    runner_id: int = 0
    name: str = ""
    status: str = "offline"
    busy: bool = False
    os: str = ""
    labels: List[GitHubLabel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GitHubRunner":
        """Deserialize GitHubRunner from a plain dictionary."""
        source = data or {}
        labels_in = source.get("labels", [])
        labels = [
            GitHubLabel.from_dict(label)
            for label in labels_in
            if isinstance(label, dict)
        ]
        return cls(
            runner_id=int(source.get("id", 0)),
            name=str(source.get("name", "")),
            status=str(source.get("status", "offline")),
            busy=bool(source.get("busy", False)),
            os=str(source.get("os", "")),
            labels=labels,
        )

    def label_names(self) -> List[str]:
        """Return label names in API order, excluding empty names."""
        return [label.name for label in self.labels if label.name]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize GitHubRunner into a dictionary."""
        return {
            "id": self.runner_id,
            "name": self.name,
            "status": self.status,
            "busy": self.busy,
            "os": self.os,
            "labels": [label.to_dict() for label in self.labels],
        }


@dataclass
class GitHubRunnersPayload:
    """Top-level payload from the GitHub Actions runners API."""

    runners: List[GitHubRunner] = field(default_factory=list)
    total_count: int = 0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GitHubRunnersPayload":
        """Deserialize GitHubRunnersPayload from a plain dictionary."""
        source = data or {}
        runners_in = source.get("runners", [])
        runners = [
            GitHubRunner.from_dict(runner)
            for runner in runners_in
            if isinstance(runner, dict)
        ]
        return cls(runners=runners, total_count=int(source.get("total_count", len(runners))))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize GitHubRunnersPayload into a dictionary."""
        return {
            "runners": [runner.to_dict() for runner in self.runners],
            "total_count": self.total_count,
        }


@dataclass
class LastNotification:
    """Stored notification metadata used for message deduplication."""

    offline_set: List[str] = field(default_factory=list)
    message_id: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "LastNotification":
        """Deserialize LastNotification from a plain dictionary."""
        source = data or {}
        offline = source.get("offline_set", [])
        offline_list = [item for item in offline if isinstance(item, str)]
        return cls(
            offline_set=offline_list,
            message_id=str(source.get("message_id", "")),
            updated_at=str(source.get("updated_at", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize LastNotification into a dictionary."""
        if not self.offline_set and not self.message_id and not self.updated_at:
            return {}
        return {
            "offline_set": list(self.offline_set),
            "message_id": self.message_id,
            "updated_at": self.updated_at,
        }

    def clone(self) -> "LastNotification":
        """Create a deep copy of the notification metadata."""
        return LastNotification(
            offline_set=list(self.offline_set),
            message_id=self.message_id,
            updated_at=self.updated_at,
        )


@dataclass
class RunnerState:
    """Persisted status fields for one host."""

    status: RunnerStatus = RunnerStatus.UNKNOWN
    consecutive_offline: int = 0
    consecutive_missing: int = 0
    labels: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RunnerState":
        """Deserialize RunnerState from a plain dictionary."""
        source = data or {}
        return cls(
            status=RunnerStatus.from_value(source.get("status", RunnerStatus.UNKNOWN.value)),
            consecutive_offline=int(source.get("consecutive_offline", 0)),
            consecutive_missing=int(source.get("consecutive_missing", 0)),
            labels=str(source.get("labels", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize RunnerState into a dictionary."""
        return {
            "status": self.status.value,
            "consecutive_offline": self.consecutive_offline,
            "consecutive_missing": self.consecutive_missing,
            "labels": self.labels,
        }


@dataclass
class MonitorState:
    """Persisted monitor state across runs."""

    last_run: str = ""
    runners: Dict[str, RunnerState] = field(default_factory=dict)
    last_notification: LastNotification = field(default_factory=LastNotification)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MonitorState":
        """Deserialize MonitorState and normalize missing host entries."""
        source = data or {}
        runners_in = source.get("runners", {})
        runners: Dict[str, RunnerState] = {}
        for host in CANONICAL_HOSTS:
            runners[host] = RunnerState.from_dict(runners_in.get(host))
        return cls(
            last_run=str(source.get("last_run", "")),
            runners=runners,
            last_notification=LastNotification.from_dict(
                source.get("last_notification")
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize MonitorState into a dictionary."""
        return {
            "last_run": self.last_run,
            "runners": {host: self.runners[host].to_dict() for host in CANONICAL_HOSTS},
            "last_notification": self.last_notification.to_dict(),
        }


@dataclass
class HistoryEntry:
    """One historical state sample for a host."""

    timestamp: str
    state: SampleState

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["HistoryEntry"]:
        """Deserialize one history entry if required fields are present."""
        source = data or {}
        timestamp = source.get("timestamp")
        state = SampleState.from_value(source.get("state"))
        if not isinstance(timestamp, str) or state is None:
            return None
        return cls(timestamp=timestamp, state=state)

    def to_dict(self) -> Dict[str, str]:
        """Serialize HistoryEntry into a dictionary."""
        return {"timestamp": self.timestamp, "state": self.state.value}


@dataclass
class RunnerStats:
    """Persisted history and labels for one host."""

    history: List[HistoryEntry] = field(default_factory=list)
    labels: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RunnerStats":
        """Deserialize RunnerStats from a plain dictionary."""
        source = data or {}
        history_entries: List[HistoryEntry] = []
        for entry in source.get("history", []):
            parsed = HistoryEntry.from_dict(entry)
            if parsed is not None:
                history_entries.append(parsed)
        return cls(history=history_entries, labels=str(source.get("labels", "")))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize RunnerStats into a dictionary."""
        return {
            "history": [entry.to_dict() for entry in self.history],
            "labels": self.labels,
        }


@dataclass
class MonitorStats:
    """Persisted monitoring statistics across runs."""

    runners: Dict[str, RunnerStats] = field(default_factory=dict)
    last_cleanup: str = ""

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MonitorStats":
        """Deserialize MonitorStats and normalize missing host entries."""
        source = data or {}
        runners_in = source.get("runners", {})
        runners: Dict[str, RunnerStats] = {}
        for host in CANONICAL_HOSTS:
            runners[host] = RunnerStats.from_dict(runners_in.get(host))
        return cls(runners=runners, last_cleanup=str(source.get("last_cleanup", "")))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize MonitorStats into a dictionary."""
        return {
            "runners": {host: self.runners[host].to_dict() for host in CANONICAL_HOSTS},
            "last_cleanup": self.last_cleanup,
        }


@dataclass(frozen=True)
class MonitorRunResult:
    """Outputs produced by a single monitoring pass."""

    state: MonitorState
    stats: MonitorStats
    should_notify: bool
    message: str
    offline_set: List[str]
    should_edit: bool
    last_message_id: str

    @property
    def new_state(self) -> Dict[str, Any]:
        """Compatibility view of state as a plain dictionary."""
        return self.state.to_dict()

    @property
    def new_stats(self) -> Dict[str, Any]:
        """Compatibility view of stats as a plain dictionary."""
        return self.stats.to_dict()
