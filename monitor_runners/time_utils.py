"""Timestamp parsing/formatting helpers for persisted monitor data."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_timestamp(timestamp: str) -> datetime:
    """Parse an ISO UTC timestamp used in persisted files."""
    return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def format_timestamp(moment: datetime) -> str:
    """Format a datetime as the canonical ISO UTC string."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
