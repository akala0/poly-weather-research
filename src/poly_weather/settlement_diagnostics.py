"""Bounded public projections written durably before rule processing."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from poly_weather.domain import SettlementSpec
from poly_weather.settlement import (
    PARSER_VERSION,
    parse_settlement_evidence,
    verify_settlement_evidence,
)

PUBLIC_FIELDS = frozenset(
    (
        "id",
        "slug",
        "title",
        "question",
        "description",
        "resolutionSource",
        "category",
        "conditionId",
        "active",
        "closed",
        "endDate",
        "outcomes",
        "outcomePrices",
        "clobTokenIds",
    )
)


class DiagnosticWriteError(OSError):
    """Missing durable diagnostics must stop startup/reconciliation."""


def safe_exception(exc: Exception) -> dict:
    message = re.sub(r"https?://\S+", "[URL]", str(exc))
    message = re.sub(
        r"(?i)(token|password|secret|authorization|api.key)\s*[=:]\s*\S+", "[REDACTED]", message
    )
    return {"type": type(exc).__name__, "reason": message[:500]}


def public_projection(payload: dict) -> dict:
    result = {k: v for k, v in payload.items() if k in PUBLIC_FIELDS}
    if "markets" in payload:
        result["markets"] = [
            public_projection(m) for m in payload["markets"] if isinstance(m, dict)
        ]
    # These are public-rule URLs; reject embedded credentials rather than copying them.
    for field in ("resolutionSource",):
        if isinstance(result.get(field), str) and re.search(r"https?://[^/]*@", result[field]):
            result[field] = "REDACTED_CREDENTIAL_URL"
    return result


class SettlementDiagnostics:
    def __init__(self, data_dir: Path, attempt_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", attempt_id):
            raise ValueError("invalid diagnostic attempt id")
        self.root = (
            data_dir
            / "logs"
            / "daemons"
            / "attempts"
            / "market-supervisor"
            / attempt_id
            / "settlement"
        )
        self.attempt_id = attempt_id
        self.spec: SettlementSpec | None = None
        self.target: date | None = None
        self.inputs: dict[str, dict] = {}

    def write(self, record: dict) -> None:
        record = {
            "attempt_id": self.attempt_id,
            "parser_version": PARSER_VERSION,
            "projection_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            **record,
        }
        try:
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).encode()
            if len(encoded) > 2_000_000:
                raise ValueError("diagnostic projection exceeds 2 MB limit")
            self.root.mkdir(parents=True, exist_ok=True)
            # Unique immutable records preserve an already confirmed prefix on later failure.
            with (self.root / f"{uuid4().hex}.json").open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, ValueError, TypeError) as exc:
            raise DiagnosticWriteError(
                f"settlement diagnostic write failed: {type(exc).__name__}"
            ) from exc

    def begin(self, spec: SettlementSpec, target: date) -> None:
        self.spec, self.target, self.inputs = spec, target, {}

    def observe(
        self, stage: str, payload: dict, receipt: datetime, error: Exception | None = None
    ) -> None:
        assert self.spec is not None
        event_id = str(payload.get("id", ""))
        slug = str(payload.get("slug", ""))
        matches = re.fullmatch(self.spec.market_slug_pattern, slug) is not None
        projection = public_projection(payload)
        digest = hashlib.sha256(
            json.dumps(projection, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        row = {
            "station_id": self.spec.station_id,
            "local_target_date": str(self.target),
            "event_id": event_id,
            "event_slug": slug,
            "receipt_at": receipt.isoformat(),
            "projection_sha256": digest,
            "stage": stage,
            "matches_registry_pattern": matches,
        }
        if stage == "input":
            row["source_projection"] = projection
            self.inputs[event_id] = row
        if error is not None:
            # Exception text may contain arbitrary response data. Preserve type and safe category.
            row["exception"] = safe_exception(error)
        self.write(row)

    def discovery_outcome(self, outcome: str, **details: Any) -> None:
        self.write(
            {
                "station_id": self.spec.station_id if self.spec else None,
                "local_target_date": str(self.target),
                "stage": "discovery",
                "outcome": outcome,
                **details,
            }
        )

    def evaluate(self, event, spec: SettlementSpec):
        if event.event_id not in self.inputs:
            # Reconcile may receive a next-day event. Derive its target from its slug,
            # never from server UTC receipt date.
            from poly_weather.settlement import _target_date

            target = _target_date(event, str(event.raw_payload.get("description") or ""))
            self.begin(spec, target)
            self.observe(
                "input",
                event.raw_payload | {"id": event.event_id, "slug": event.event_slug},
                event.fetched_at,
            )
        base = {k: v for k, v in self.inputs[event.event_id].items() if k != "source_projection"}
        try:
            evidence = parse_settlement_evidence(event, registry_spec=spec)
        except Exception as exc:
            self.write(
                base
                | {"stage": "parse", "outcome": "parse_failed", "exception": safe_exception(exc)}
            )
            return None, None
        try:
            verification = verify_settlement_evidence(evidence, spec)
        except Exception as exc:
            self.write(
                base
                | {
                    "stage": "verification",
                    "outcome": "verification_error",
                    "exception": safe_exception(exc),
                }
            )
            return evidence, None
        outcome = (
            "passed"
            if verification.passed
            else "rule_incomplete"
            if evidence.missing_fields
            else "registry_mismatch"
        )
        self.write(
            base
            | {
                "stage": "verification",
                "outcome": outcome,
                "rule_schema_version": evidence.rule_schema_version,
                "evidence": evidence.model_dump(mode="json"),
                "verification": verification.model_dump(mode="json"),
                "registry_expected": spec.model_dump(mode="json"),
            }
        )
        return evidence, verification
