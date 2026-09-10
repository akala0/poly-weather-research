"""Content-bound positions shared by plain/gzip followers and restart readers."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from poly_weather.archive_io import ArchiveRepresentationError


def logical_archive(path: Path) -> str:
    return str((path.with_suffix("") if path.suffix == ".gz" else path).resolve())


def _identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def read_positioned_rows(
    path: Path, position: Mapping[str, Any], *, committed_only: bool = False,
    visible: Callable[[Mapping[str, Any]], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate before publishing a position; offsets count decompressed bytes.

    Legacy nonzero positions lack a durable prefix digest and remain UNKNOWN.
    No migration can safely infer their content from today's replacement file.
    """
    offset = int(position.get("offset", 0))
    if offset < 0:
        raise ArchiveRepresentationError("INVALID_ARCHIVE_OFFSET")
    versioned = position.get("archive_position_schema") == 2
    if not versioned and (offset or position.get("line") or position.get("skip_existing_gzip")):
        raise ArchiveRepresentationError("UNKNOWN_ARCHIVE_PREFIX")
    if versioned and position.get("logical_archive") != logical_archive(path):
        raise ArchiveRepresentationError("ARCHIVE_SCOPE_MISMATCH")
    digest = hashlib.sha256()
    prefix_lines = 0
    last_byte = b""
    retained = bytearray()
    try:
        before = _identity(path)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as handle:
            remaining = offset
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ArchiveRepresentationError("ARCHIVE_PREFIX_TRUNCATED")
                digest.update(chunk)
                prefix_lines += chunk.count(b"\n")
                last_byte = chunk[-1:]
                if committed_only:
                    retained.extend(chunk)
                remaining -= len(chunk)
            if versioned and digest.hexdigest() != position.get("prefix_sha256"):
                raise ArchiveRepresentationError("ARCHIVE_PREFIX_CHANGED")
            if versioned and (prefix_lines != position.get("line") or (offset and last_byte != b"\n")):
                raise ArchiveRepresentationError("ARCHIVE_PREFIX_BOUNDARY_MISMATCH")
            tail = b"" if committed_only else handle.read()
        if before != _identity(path):
            raise ArchiveRepresentationError("ARCHIVE_CHANGED_DURING_READ")
    except (OSError, EOFError) as exc:
        raise ArchiveRepresentationError("ARCHIVE_UNREADABLE") from exc
    complete = tail[:tail.rfind(b"\n") + 1]
    payload = bytes(retained) if committed_only else complete
    rows = []
    admitted_bytes = 0
    for line in payload.splitlines(keepends=True):
        if not line.strip():
            admitted_bytes += len(line)
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveRepresentationError("INVALID_COMPLETE_ARCHIVE_ROW") from exc
        if not isinstance(value, dict):
            raise ArchiveRepresentationError("INVALID_ARCHIVE_ROW_TYPE")
        if visible is not None and not committed_only and not visible(value):
            break  # Do not acknowledge a row before its receipt is visible.
        rows.append(value)
        admitted_bytes += len(line)
    if committed_only:
        return rows, dict(position)
    complete = complete[:admitted_bytes]
    digest.update(complete)
    updated = dict(position)
    updated.update(archive_position_schema=2, logical_archive=logical_archive(path),
                   representation=str(path.resolve()), representation_identity=list(before),
                   offset=offset + len(complete), line=int(position.get("line", 0)) + complete.count(b"\n"),
                   prefix_sha256=digest.hexdigest())
    return rows, updated
