"""Transparent readers for plain and retention-compressed JSONL archives."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
from typing import IO


class ArchiveRepresentationError(ValueError):
    """Two representations cannot be silently treated as independent evidence."""


def _content_digest(path: Path) -> str:
    value = hashlib.sha256()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def jsonl_archive_paths(root: Path) -> list[Path]:
    paths = sorted((*root.glob("*/events.jsonl"), *root.glob("*/events.jsonl.gz")))
    selected = []
    for path in paths:
        plain = path.with_suffix("") if path.suffix == ".gz" else None
        if plain is not None and plain in paths:
            before = [(p.stat().st_size, p.stat().st_mtime_ns) for p in (plain, path)]
            try:
                same = _content_digest(plain) == _content_digest(path)
            except (OSError, EOFError) as exc:
                raise ArchiveRepresentationError("unreadable duplicate archive representations") from exc
            after = [(p.stat().st_size, p.stat().st_mtime_ns) for p in (plain, path)]
            if not same or before != after:
                raise ArchiveRepresentationError("conflicting or changing duplicate archive representations")
            continue  # Equal bytes: choose plain without deleting either object.
        selected.append(path)
    return selected


def open_jsonl_text(path: Path) -> IO[str]:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")
