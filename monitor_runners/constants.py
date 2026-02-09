"""Shared constants and name-matching utilities for runner monitoring."""

from __future__ import annotations

import re
from typing import Optional, Tuple

# hoskinson, hoskinson1, ..., hoskinson9
CANONICAL_HOSTS: Tuple[str, ...] = ("hoskinson",) + tuple(
    f"hoskinson{i}" for i in range(1, 10)
)
_CANONICAL_HOST_SET = set(CANONICAL_HOSTS)
_RUNNER_NAME_RE = re.compile(r"^(hoskinson(?:[1-9])?)(?:-.*)?$")


def host_for_name(name: str) -> Optional[str]:
    """Map a runner name to a host, or return None if unsupported."""
    match = _RUNNER_NAME_RE.fullmatch(name)
    if match is None:
        return None
    host = match.group(1)
    if host in _CANONICAL_HOST_SET:
        return host
    return None
