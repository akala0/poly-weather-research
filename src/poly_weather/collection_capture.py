"""Opt-in bounded public wire recorder; no legacy archive, DB, status or strategy output.

Only the public Gamma exact-event GET and public market WebSocket are used. This
module deliberately does not construct MarketWebSocketBot or ResearchWarehouse.
Structural book initialization is NOT healthy L2 or a trade-completeness claim.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import connect

from poly_weather.collection_identity import (
    CollectionIdentity,
    IdentityError,
    digest,
    identify_events,
)
from poly_weather.config import load_settlement_registry
from poly_weather.public_trade_collection import _writer_lock

DOMAIN = "isolated-public-market-capture/v1"
PROJECT = Path(__file__).resolve().parents[2]
PUBLIC_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com/events/slug/"


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    station_id: str = Field(pattern=r"^[A-Z0-9]{4}$")
    target_date: date
    event_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=160)


class CapturePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selections: tuple[Selection, ...] = Field(min_length=1, max_length=20)
    runtime_seconds: int = Field(ge=1, le=1800)
    max_tokens: int = Field(ge=2, le=512)
    max_bytes: int = Field(ge=1024 * 1024, le=2 * 1024**3)
    min_free_bytes: int = Field(ge=1024 * 1024)
    max_frame_bytes: int = Field(default=16 * 1024**2, ge=1024, le=16 * 1024**2)
    max_frames: int = Field(default=100_000, ge=1, le=1_000_000)
    max_connection_failures: int = Field(default=3, ge=1, le=3)


def safe_root(root: Path) -> Path:
    """Reject aliases/ancestors/descendants of production data before creating anything."""
    candidate = root.absolute()
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("capture_root_link_forbidden")
    resolved = candidate.resolve()
    forbidden = (PROJECT / "data").resolve()
    if (
        resolved == forbidden
        or resolved.is_relative_to(forbidden)
        or forbidden.is_relative_to(resolved)
    ):
        raise ValueError("capture_root_overlaps_production")
    if resolved == Path(resolved.anchor):
        raise ValueError("capture_root_is_filesystem_root")
    return resolved


def _write_new(path: Path, value: dict) -> None:
    encoded = (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()
    with path.open("xb") as handle:
        if handle.write(encoded) != len(encoded):
            raise OSError("short_capture_control_write")
        handle.flush()
        os.fsync(handle.fileno())


class CaptureStore:
    """One immutable hash-chained segment per run; crash tails block same-root restart.

    Caller holds the global cooperating-writer lock for the complete lifetime.
    No automatic truncation, archive deletion, compression or old cursor migration.
    """

    def __init__(self, root: Path, plan: CapturePlan):
        self.root = safe_root(root)
        self.plan = plan
        self.halted = False
        self.sequence = 0
        self.previous = "0" * 64
        self.bytes = 0
        self.root_bytes = 0
        if self.root.exists():
            if not self.root.is_dir():
                raise ValueError("capture_root_not_directory")
            # Refuse nested aliases too; a safe parent alone doesn't secure output children.
            for item in self.root.rglob("*"):
                if item.is_symlink() or item.is_junction():
                    raise ValueError("capture_child_link_forbidden")
                if item.is_file():
                    self.root_bytes += item.stat().st_size
                if self.root_bytes >= plan.max_bytes:
                    raise ValueError("capture_root_budget_exceeded")
            if not (self.root / "capture-root.json").is_file():
                if any(self.root.iterdir()):
                    raise ValueError("non_capture_root_not_empty")
            else:
                if json.loads((self.root / "capture-root.json").read_bytes()) != {"domain": DOMAIN}:
                    raise ValueError("capture_root_schema_conflict")
                allowed_root = {"capture-root.json", "runs"}
                if any(item.name not in allowed_root for item in self.root.iterdir()):
                    raise ValueError("capture_root_schema_conflict")
                runs_root = self.root / "runs"
                if runs_root.exists() and (runs_root.is_symlink() or runs_root.is_junction()):
                    raise ValueError("capture_runs_link_forbidden")
                if runs_root.exists() and not runs_root.is_dir():
                    raise ValueError("capture_runs_not_directory")
                if not runs_root.exists():
                    runs_root.mkdir(parents=False, exist_ok=False)
                for run in runs_root.iterdir():
                    if run.is_symlink() or run.is_junction() or not run.is_dir():
                        raise ValueError("capture_run_path_conflict")
                    allowed_run = {
                        "start.capture.json",
                        "frames.capture.jsonl",
                        "collection-membership.capture.json",
                        "result.capture.json",
                    }
                    if any(item.name not in allowed_run for item in run.iterdir()):
                        raise ValueError("capture_run_schema_conflict")
                    start = run / "start.capture.json"
                    if not start.is_file():
                        raise ValueError("unconfirmed_prior_capture_run")
                    started = json.loads(start.read_bytes())
                    if (
                        started.get("domain") != DOMAIN
                        or started.get("run_id") != run.name
                        or started.get("attempt_id") != run.name
                    ):
                        raise ValueError("capture_attempt_identity_conflict")
                    result = run / "result.capture.json"
                    if not result.is_file():
                        raise ValueError("unconfirmed_prior_capture_run")
                    completed = json.loads(result.read_bytes())
                    if (
                        completed.get("domain") != DOMAIN
                        or completed.get("run_id") != run.name
                        or completed.get("outcome") != "complete"
                    ):
                        raise ValueError("failed_prior_capture_run_requires_review")
                    try:
                        prior = verify_segment(
                            run / "frames.capture.jsonl", max_bytes=plan.max_bytes
                        )
                    except (OSError, KeyError, TypeError, ValueError) as exc:
                        raise ValueError("prior_capture_segment_unverifiable") from exc
                    if (prior["sequence"], prior["digest"]) != (
                        completed["sequence"],
                        completed["digest"],
                    ):
                        raise ValueError("prior_capture_frontier_conflict")
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / "capture-root.json").exists():
            _write_new(self.root / "capture-root.json", {"domain": DOMAIN})
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        self.run_id = uuid4().hex
        self.directory = self.root / "runs" / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        _write_new(
            self.directory / "start.capture.json",
            {
                "domain": DOMAIN,
                "run_id": self.run_id,
                "attempt_id": self.run_id,
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
                "plan": plan.model_dump(mode="json"),
                "plan_sha256": digest(plan.model_dump(mode="json")),
                "strategy_admitted": False,
            },
        )
        self.root_bytes = sum(
            item.stat().st_size for item in self.root.rglob("*") if item.is_file()
        )
        self.handle = (self.directory / "frames.capture.jsonl").open("xb")

    def append(self, kind: str, record: dict) -> None:
        if self.halted:
            raise OSError("capture_store_halted")
        body = {
            "capture_domain": DOMAIN,
            "run": self.run_id,
            "ordinal": self.sequence + 1,
            "previous_digest": self.previous,
            "kind": kind,
            "record": record,
        }
        checksum = digest(body)
        encoded = (
            json.dumps({**body, "digest": checksum}, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode()
        try:
            # Reserve bounded control-record space; the quota includes prior runs.
            if self.root_bytes + len(encoded) + 65536 > self.plan.max_bytes:
                raise OSError("capture_byte_budget_exceeded")
            if shutil.disk_usage(self.root).free < self.plan.min_free_bytes + len(encoded):
                raise OSError("capture_disk_reserve_exceeded")
            if self.handle.write(encoded) != len(encoded):
                raise OSError("short_capture_frame_write")
            self.handle.flush()
            os.fsync(self.handle.fileno())
        except BaseException:
            self.halted = True
            raise
        self.sequence += 1
        self.previous = checksum
        self.bytes += len(encoded)
        self.root_bytes += len(encoded)

    def wire(
        self,
        kind: str,
        payload: str | bytes,
        *,
        receipt_ns: int,
        epoch: int,
        source_url: str = PUBLIC_WS,
    ) -> None:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.append(
            kind,
            {
                "receipt_ns": receipt_ns,
                "connection_epoch": epoch,
                "source_url": source_url,
                "representation": "utf8_text" if isinstance(payload, str) else "binary",
                "payload_bytes_base64": base64.b64encode(data).decode(),
                "payload_sha256": hashlib.sha256(data).hexdigest(),
                "healthy_l2": False,
                "strategy_admitted": False,
            },
        )

    def write_membership(self, identity: CollectionIdentity) -> None:
        _write_new(
            self.directory / "collection-membership.capture.json",
            {
                "domain": DOMAIN,
                "run_id": self.run_id,
                "collection_admitted": True,
                "strategy_approved": False,
                "settlement_rule_state": "UNKNOWN_OR_INCOMPLETE",
                "identity_sha256": identity.identity_sha256,
                "binding_count": len(identity.bindings),
                "healthy_l2": False,
                "group_completeness": "UNSUPPORTED",
            },
        )

    def finish(self, outcome: str, *, error_type: str | None = None) -> None:
        if outcome == "complete" and self.halted:
            raise OSError("capture_store_halted")
        try:
            self.handle.close()
            # Publish only after the complete bytes have passed flush/fsync.
            # A failed finish retains its pending file and blocks same-root restart.
            pending = self.directory / "result.pending.capture.json"
            _write_new(
                pending,
                {
                    "domain": DOMAIN,
                    "run_id": self.run_id,
                    "outcome": outcome,
                    "error_type": error_type,
                    "sequence": self.sequence,
                    "digest": self.previous,
                    "committed_bytes": self.bytes,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "healthy_l2": False,
                    "strategy_admitted": False,
                },
            )
            pending.rename(self.directory / "result.capture.json")
        finally:
            self.halted = True


def verify_segment(path: Path, *, max_bytes: int) -> dict:
    """Read-only verifier, not a legacy-schema exporter or automatic tail repair."""
    if path.is_symlink() or path.stat().st_size > max_bytes:
        raise ValueError("capture_segment_path_or_size")
    previous, sequence = "0" * 64, 0
    with path.open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                raise ValueError("unconfirmed_capture_tail")
            value = json.loads(line)
            checksum = value.pop("digest")
            if (
                checksum != digest(value)
                or value.get("capture_domain") != DOMAIN
                or value.get("ordinal") != sequence + 1
                or value.get("previous_digest") != previous
            ):
                raise ValueError("capture_chain_conflict")
            previous, sequence = checksum, sequence + 1
    return {"sequence": sequence, "digest": previous}


class SnapshotBoundary:
    """Structural legacy-wire observations only; no healthy-book/strategy conversion."""

    def __init__(self, identity: CollectionIdentity):
        self.bindings = {b.token_id: b for b in identity.bindings}
        self.initialized: set[str] = set()

    def reset(self) -> None:
        self.initialized.clear()

    @staticmethod
    def _levels(value: Any) -> bool:
        if not isinstance(value, list):
            return False
        seen = set()
        for level in value:
            if not isinstance(level, dict):
                return False
            try:
                p, q = Decimal(str(level["price"])), Decimal(str(level["size"]))
                if not p.is_finite() or not q.is_finite() or not 0 <= p <= 1 or q < 0 or p in seen:
                    return False
                seen.add(p)
            except (ValueError, ArithmeticError, KeyError):
                return False
        return True

    def observe(self, payload: str | bytes) -> dict:
        reasons = []
        try:
            decoded = json.loads(payload)
            rows = decoded if isinstance(decoded, list) else [decoded]
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("unknown_protocol")
                kind = row.get("event_type")
                if kind == "book":
                    token = row.get("asset_id")
                    binding = self.bindings.get(token)
                    if binding is None or row.get("market") != binding.condition_id:
                        raise ValueError("token_condition_conflict")
                    if not self._levels(row.get("bids")) or not self._levels(row.get("asks")):
                        raise ValueError("full_snapshot_missing_or_invalid_sides")
                    self.initialized.add(token)
                elif kind == "price_change":
                    changes = row.get("price_changes")
                    if not isinstance(changes, list) or not changes:
                        raise ValueError("unknown_delta_format")
                    for change in changes:
                        if not isinstance(change, dict):
                            raise ValueError("unknown_delta_format")
                        token = change.get("asset_id")
                        binding = self.bindings.get(token)
                        if binding is None or row.get("market") != binding.condition_id:
                            raise ValueError("token_condition_conflict")
                        if change.get("side") not in {"BUY", "SELL"} or not self._levels([change]):
                            raise ValueError("invalid_delta")
                        if token not in self.initialized:
                            reasons.append("delta_before_full_snapshot")
                else:
                    # Including newer protocol formats and lifecycle: retain raw, don't infer L2.
                    raise ValueError("unsupported_protocol_or_message")
        except (ValueError, TypeError, KeyError) as exc:
            self.reset()
            reasons.append(str(exc) if type(exc) is ValueError else "unrecognized_payload")
        return {
            "initialized_tokens": sorted(self.initialized),
            "reasons": reasons,
            "healthy_l2": False,
            "continuity": "UNKNOWN",
            "group_completeness": "UNSUPPORTED",
        }


async def fetch_public_event(slug: str) -> tuple[bytes, int]:
    """Bounded exact-event public GET, no redirects/retries/credentials."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("invalid_event_slug")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        async with client.stream("GET", GAMMA + slug) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2 * 1024**2:
                    raise ValueError("gamma_response_limit")
            return bytes(body), time.time_ns()


async def run_capture(
    *,
    enabled: bool,
    root: Path,
    plan: CapturePlan,
    registry_path: Path,
    fetcher=fetch_public_event,
    connector=connect,
) -> dict:
    """Finite exact-event capture. No discovery expansion, rolling supervisor or child process."""
    if not enabled:
        raise ValueError("capture_disabled_requires_explicit_enable")
    started = time.monotonic()
    root = safe_root(root)
    specs = load_settlement_registry(registry_path).specs
    chosen = []
    for selection in plan.selections:
        matching = [s for s in specs if s.station_id == selection.station_id]
        if len(matching) != 1:
            raise IdentityError("ambiguous_or_missing_station_spec")
        chosen.append((selection, matching[0]))
    # One recorder per host, including differing output roots. OS releases on crash.
    lock = Path(tempfile.gettempdir()) / "poly-weather-isolated-market-capture.lock"
    if lock.is_symlink() or lock.is_junction():
        raise ValueError("capture_lock_alias")
    with _writer_lock(lock):
        store = CaptureStore(root, plan)
        epoch, failures, frames = 0, 0, 0
        identity_hash = None
        phase = "startup"
        active_selection: dict[str, str] | None = None
        try:
            while time.monotonic() - started < plan.runtime_seconds:
                epoch += 1
                selections = []
                # Re-read and retain identity/rules at every connection. No old snapshot subscription.
                for selection, spec in chosen:
                    active_selection = {
                        "station_id": selection.station_id,
                        "target_date": selection.target_date.isoformat(),
                        "event_slug": selection.event_slug,
                    }
                    phase = "identity_fetch"
                    remaining = plan.runtime_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("capture_budget_before_identity")
                    data, receipt = await asyncio.wait_for(
                        fetcher(selection.event_slug), timeout=min(15, remaining)
                    )
                    store.wire(
                        "public_event_response",
                        data,
                        receipt_ns=receipt,
                        epoch=epoch,
                        source_url=GAMMA + selection.event_slug,
                    )
                    phase = "identity_decode"
                    raw = json.loads(data)
                    if not isinstance(raw, dict) or raw.get("slug") != selection.event_slug:
                        raise IdentityError("requested_event_conflict")
                    selections.append((raw, spec, selection.target_date))
                active_selection = None
                phase = "identity_validation"
                identity = identify_events(selections, max_tokens=plan.max_tokens)
                store.append(
                    "identity_and_rules",
                    {
                        "connection_epoch": epoch,
                        "identity_sha256": identity.identity_sha256,
                        "projection_sha256": identity.projection_sha256,
                        "bindings": [asdict(b) for b in identity.bindings],
                        "rule_results": identity.rule_results,
                        "strategy_admitted": False,
                    },
                )
                if identity_hash is not None and identity_hash != identity.identity_sha256:
                    raise IdentityError("identity_changed_requires_new_review")
                if identity_hash is None:
                    store.write_membership(identity)
                identity_hash = identity.identity_sha256
                boundary = SnapshotBoundary(identity)
                phase = "connection_open"
                remaining = plan.runtime_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("capture_budget_before_connect")
                try:
                    async with asyncio.timeout(remaining):
                        async with connector(
                            PUBLIC_WS,
                            ping_interval=None,
                            compression=None,
                            max_size=plan.max_frame_bytes,
                            max_queue=16,
                            open_timeout=min(10, remaining),
                            close_timeout=2,
                        ) as ws:
                            await ws.send(
                                json.dumps(
                                    {
                                        "assets_ids": [b.token_id for b in identity.bindings],
                                        "type": "market",
                                        "custom_feature_enabled": True,
                                    }
                                )
                            )
                            phase = "subscription"
                            store.append(
                                "subscription_sent",
                                {"connection_epoch": epoch, "identity_sha256": identity_hash},
                            )
                            last_ping = time.monotonic()
                            while time.monotonic() - started < plan.runtime_seconds:
                                phase = "frame_receive"
                                if frames >= plan.max_frames:
                                    raise OSError("capture_frame_budget_exceeded")
                                if time.monotonic() - last_ping >= 10:
                                    await ws.send("PING")
                                    last_ping = time.monotonic()
                                try:
                                    payload = await asyncio.wait_for(ws.recv(), timeout=1)
                                except TimeoutError:
                                    continue
                                receipt = time.time_ns()
                                if not isinstance(payload, (str, bytes)):
                                    raise OSError("unexpected_transport_type")
                                size = (
                                    len(payload.encode())
                                    if isinstance(payload, str)
                                    else len(payload)
                                )
                                if size > plan.max_frame_bytes:
                                    wire = payload.encode() if isinstance(payload, str) else payload
                                    store.append(
                                        "oversize_frame",
                                        {
                                            "connection_epoch": epoch,
                                            "receipt_ns": receipt,
                                            "payload_size": size,
                                            "payload_sha256": hashlib.sha256(wire).hexdigest(),
                                            "payload_prefix_base64": base64.b64encode(
                                                wire[: plan.max_frame_bytes]
                                            ).decode(),
                                            "truncated": True,
                                            "healthy_l2": False,
                                        },
                                    )
                                    raise OSError("capture_frame_limit")
                                store.wire(
                                    "public_market_frame", payload, receipt_ns=receipt, epoch=epoch
                                )
                                frames += 1
                                store.append(
                                    "structural_observation",
                                    {
                                        "connection_epoch": epoch,
                                        "frame_count": frames,
                                        **boundary.observe(payload),
                                    },
                                )
                except TimeoutError:
                    if time.monotonic() - started < plan.runtime_seconds:
                        raise
                    break
                except OSError:
                    # Includes persistence failures: never reconnect after a failed durable write.
                    raise
                except Exception as exc:
                    boundary.reset()
                    failures += 1
                    store.append(
                        "connection_gap",
                        {
                            "connection_epoch": epoch,
                            "phase": phase,
                            "error_type": type(exc).__name__,
                            "failures": failures,
                            "healthy_l2": False,
                        },
                    )
                    if failures >= plan.max_connection_failures:
                        raise
            if identity_hash is None:
                raise TimeoutError("capture_budget_before_identity")
            store.finish("complete")
            return {
                "capture_root": str(root),
                "run_id": store.run_id,
                "frames": frames,
                "connection_failures": failures,
                "healthy_l2": False,
                "strategy_admitted": False,
            }
        except BaseException as exc:
            try:
                if not store.halted:
                    store.append(
                        "failure",
                        {
                            "phase": phase,
                            "selection": active_selection,
                            "error_type": type(exc).__name__,
                            "reason": str(exc)[:200]
                            if isinstance(exc, IdentityError)
                            else "capture_failed",
                        },
                    )
            finally:
                store.finish("failed", error_type=type(exc).__name__)
            raise
