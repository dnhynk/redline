"""Per-endpoint call counters.

The IO layer counts; it does not price and it does not decide. ``snapshot()``
is the shape the caller puts in its budget envelope under ``endpoint_calls``.

``calls`` counts every attempt including cache hits and failures, so
``calls - cache_hits - failures`` is the number of requests that actually
reached the network and returned something usable.
"""

from __future__ import annotations

import threading

from tools.schemas import ENDPOINTS

_LOCK = threading.Lock()
_COUNTS: dict[str, dict[str, int]] = {
    name: {"calls": 0, "cache_hits": 0, "failures": 0} for name in ENDPOINTS
}


def record(endpoint: str, *, cache_hit: bool = False, failure: bool = False) -> None:
    """Count one call against ``endpoint``. Unknown endpoints are ignored."""
    if endpoint not in _COUNTS:
        return
    with _LOCK:
        bucket = _COUNTS[endpoint]
        bucket["calls"] += 1
        if cache_hit:
            bucket["cache_hits"] += 1
        if failure:
            bucket["failures"] += 1


def snapshot() -> dict[str, dict[str, int]]:
    """A copy of the counters, safe to serialize."""
    with _LOCK:
        return {name: dict(bucket) for name, bucket in _COUNTS.items()}


def billable_calls(endpoint: str) -> int:
    """Calls that reached the network and succeeded."""
    with _LOCK:
        bucket = _COUNTS.get(endpoint)
        if bucket is None:
            return 0
        return max(0, bucket["calls"] - bucket["cache_hits"] - bucket["failures"])


def total_calls() -> int:
    """Every counted call across all endpoints."""
    with _LOCK:
        return sum(bucket["calls"] for bucket in _COUNTS.values())


def reset() -> None:
    """Zero every counter. One run, one set of numbers."""
    with _LOCK:
        for bucket in _COUNTS.values():
            bucket["calls"] = 0
            bucket["cache_hits"] = 0
            bucket["failures"] = 0


__all__ = ["billable_calls", "record", "reset", "snapshot", "total_calls"]
