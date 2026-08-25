"""Weather collection provenance and strict no-lookahead guards."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

CollectionMode = Literal["realtime", "historical_backfill"]
REALTIME: CollectionMode = "realtime"
HISTORICAL_BACKFILL: CollectionMode = "historical_backfill"


def collection_mode(row: Mapping[str, Any]) -> CollectionMode:
    """Return explicit provenance, treating pre-migration rows as realtime."""
    value = str(row.get("collection_mode") or REALTIME)
    if value not in {REALTIME, HISTORICAL_BACKFILL}:
        raise ValueError(f"unsupported weather collection_mode: {value!r}")
    return value  # type: ignore[return-value]


def require_realtime_for_no_lookahead(
    rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Reject post-hoc QC data in any strict no-lookahead analysis path."""
    selected = list(rows)
    rejected = [row for row in selected if collection_mode(row) != REALTIME]
    if rejected:
        raise ValueError(
            "strict no-lookahead analysis refuses historical_backfill weather data"
        )
    return selected
