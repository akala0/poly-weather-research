"""Immutable receipt facts and post-fact-fsync witnesses (collector lock required).

See docs/reliability_receipt_contract.md. This is not a group-closure certificate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from poly_weather.runtime_safety import atomic_json_write


class ReceiptIntegrityError(ValueError):
    """Preserve evidence and stop when the journal cannot be reconciled."""


def materialization_is_bound(path: Path) -> bool:
    return (path.parent / ".receipt_journal" / "materializations" / digest(path.stem)).exists()


def requested_materialization_prefix(paths: Iterable[Path]) -> int | None:
    """Select a bounded read frontier; full validation still follows, never trust this hint."""
    anchors = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # The actual tape reader rejects bound malformed objects.
        anchor = payload.get("receipt_journal_anchor") if isinstance(payload, Mapping) else None
        sequence = anchor.get("sequence") if isinstance(anchor, Mapping) else None
        if type(sequence) is int and sequence > 0:
            anchors.append(sequence)
    return max(anchors) if anchors else None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _write_once(path: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    if path.exists():
        raise ReceiptIntegrityError(f"immutable object already exists: {path.name}")
    value = dict(body)
    value["checksum"] = digest(value)
    atomic_json_write(path, value, integrity_metadata=False, keep_last_good=False)
    return value


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
        checksum = value.pop("checksum")
        if checksum != digest(value):
            raise ValueError("checksum mismatch")
        value["checksum"] = checksum
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReceiptIntegrityError(f"invalid receipt object {path.name}: {exc}") from exc


class ReceiptJournal:
    def __init__(self, root: Path, clock: Callable[[], datetime], *, read_only: bool = False,
                 prefix: int | None = None, allow_pending_tail: bool = False):
        self.root = root
        self.clock = clock
        if not read_only:
            root.mkdir(parents=True, exist_ok=True)
        self.records: list[dict[str, Any]] = []
        self.pending_fact: dict[str, Any] | None = None
        self.verified_objects: dict[Path, tuple[str, int, int]] = {}
        files = sorted(root.glob("*.fact.json"))
        if prefix is not None:
            if isinstance(prefix, bool) or not isinstance(prefix, int) or prefix < 1:
                raise ReceiptIntegrityError("invalid bounded prefix")
            files = files[:prefix]
        predecessor = None
        # Validate the complete chain before writing any recovery witness.
        facts = []
        for sequence, path in enumerate(files, 1):
            value = _read(path)
            stat = path.stat()
            self.verified_objects[path] = (value["checksum"], stat.st_dev, stat.st_ino)
            if (path.name != f"{sequence:020d}.fact.json"
                    or value.get("sequence") != sequence
                    or value.get("previous_digest") != predecessor
                    or value.get("schema_version") != 1
                    or value.get("execution_enabled") is not False
                    or not isinstance(value.get("members"), list)
                    or value.get("member_count") != len(value["members"])
                    or value.get("members_digest") != digest(value.get("members"))):
                raise ReceiptIntegrityError(f"receipt chain/schema mismatch: {path.name}")
            predecessor = value["checksum"]
            facts.append(value)
        if prefix is None and any(not (root / p.name.replace(".witness.json", ".fact.json")).exists()
               for p in root.glob("*.witness.json")):
            raise ReceiptIntegrityError("orphan receipt witness")
        for fact in facts:
            witness_path = root / f"{fact['sequence']:020d}.witness.json"
            if witness_path.exists():
                witness = _read(witness_path)
                stat = witness_path.stat()
                self.verified_objects[witness_path] = (witness["checksum"], stat.st_dev, stat.st_ino)
            else:
                if allow_pending_tail and fact is facts[-1]:
                    self.pending_fact = fact
                    continue
                if read_only:
                    raise ReceiptIntegrityError("uncommitted receipt fact")
                if fact is not facts[-1]:
                    raise ReceiptIntegrityError("interior witness missing")
                # The original post-fsync clock is lost: certify a later bound.
                witness = self._witness(fact)
            if (witness.get("fact_digest") != fact["checksum"]
                    or witness.get("sequence") != fact["sequence"]
                    or witness.get("execution_enabled") is not False):
                raise ReceiptIntegrityError("receipt witness conflict")
            try:
                committed = datetime.fromisoformat(witness["receipt_committed_at"])
                response = datetime.fromisoformat(fact.get("response_received_at") or fact["failure_observed_at"])
                if committed.tzinfo is None or committed < response:
                    raise ValueError("commit before receipt")
            except (KeyError, TypeError, ValueError) as exc:
                raise ReceiptIntegrityError("invalid receipt witness clock") from exc
            self.records.append({"fact": fact, "witness": witness})

    def assert_unchanged(self) -> None:
        """No cross-call cache: recheck bytes/checksum and identity before returning a batch."""
        for path, identity in self.verified_objects.items():
            value = _read(path)
            stat = path.stat()
            if (value["checksum"], stat.st_dev, stat.st_ino) != identity:
                raise ReceiptIntegrityError("receipt changed during reader cycle")

    def recover_pending(self) -> None:
        """Call only after external anchors and existing tapes pass preflight."""
        if self.pending_fact is not None:
            fact = self.pending_fact
            self.records.append({"fact": fact, "witness": self._witness(fact)})
            self.pending_fact = None

    def commit_materialization(self, event: str, payload: Mapping[str, Any]) -> str:
        identity = digest(payload)
        path = self.root / "materializations" / digest(event) / f"{identity}.json"
        body = {"schema_version": 1, "event_slug": event, "payload": dict(payload),
                "execution_enabled": False}
        if path.exists():
            if _read(path).get("payload") != dict(payload):
                raise ReceiptIntegrityError("immutable materialization conflict")
        else:
            _write_once(path, body)
        return identity

    def _witness(self, fact: dict[str, Any]) -> dict[str, Any]:
        committed = self.clock()
        if committed < datetime.fromisoformat(fact.get("response_received_at") or fact["failure_observed_at"]):
            raise ReceiptIntegrityError("recovery clock before receipt")
        return _write_once(self.root / f"{fact['sequence']:020d}.witness.json", {
            "sequence": fact["sequence"], "fact_digest": fact["checksum"],
            "receipt_committed_at": committed.isoformat(), "execution_enabled": False,
        })

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        sequence = len(self.records) + 1
        fact = _write_once(self.root / f"{sequence:020d}.fact.json", {
            **payload, "schema_version": 1, "journal_identity": "public-receipt-v1",
            "sequence": sequence, "execution_enabled": False,
            "previous_digest": self.records[-1]["fact"]["checksum"] if self.records else None,
            "members_digest": digest(payload["members"]),
        })
        witness = self._witness(fact)
        record = {"fact": fact, "witness": witness}
        self.records.append(record)
        return record

    def verify_anchor(self, anchor: Any) -> None:
        if anchor is None:
            return
        try:
            sequence = anchor["sequence"]
            if (not isinstance(sequence, int) or sequence < 1
                    or self.records[sequence - 1]["fact"]["checksum"] != anchor["digest"]):
                raise ValueError
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ReceiptIntegrityError("receipt anchor rollback/conflict") from exc

    def discrepancy(self, details: Mapping[str, Any]) -> None:
        body = {"kind": "JOURNAL_TAPE_DISCREPANCY", **details, "execution_enabled": False}
        path = self.root / f"discrepancy-{digest(body)}.json"
        if not path.exists():
            _write_once(path, body)
