import gzip
from copy import deepcopy

import pytest

from poly_weather.archive_io import ArchiveRepresentationError, jsonl_archive_paths
from poly_weather.paper_spread_runtime import _cursor_visible_jsonl_rows
from poly_weather.shadow_runtime import ShadowCursor, _incremental_jsonl_rows


def test_ar_plain_both_gzip_two_restarts_preserve_input_ids(tmp_path):
    root = tmp_path / "raw"
    day = root / "day"
    day.mkdir(parents=True)
    plain = day / "events.jsonl"
    compressed = day / "events.jsonl.gz"
    first = '{"id":"天气"}\n\n'.encode()
    plain.write_bytes(first + b'{"id":2')
    cursor = ShadowCursor(tmp_path / "cursor.json")
    assert _incremental_jsonl_rows(plain, cursor.position(plain)) == [{"id": "天气"}]
    assert cursor.position(plain)["offset"] == len(first)
    assert cursor.position(plain)["line"] == 2
    cursor.save()
    plain.write_bytes(first + b'{"id":2}\n')
    compressed.write_bytes(gzip.compress(plain.read_bytes()))
    assert jsonl_archive_paths(root) == [plain]
    # Crash before cursor publication: reconstruction is still the old prefix.
    recovered = ShadowCursor.load(cursor.path)
    assert _cursor_visible_jsonl_rows(compressed, recovered.existing_position(compressed)) == [{"id": "天气"}]
    plain.unlink()  # Retention transition only in this temporary fixture.
    assert _incremental_jsonl_rows(compressed, recovered.position(compressed)) == [{"id": 2}]
    recovered.save()
    for _ in range(2):
        recovered = ShadowCursor.load(cursor.path)
        assert _incremental_jsonl_rows(compressed, recovered.position(compressed)) == []
        assert _cursor_visible_jsonl_rows(compressed, recovered.existing_position(compressed)) == [{"id": "天气"}, {"id": 2}]
        recovered.save()


@pytest.mark.parametrize("damage", ["truncate", "replace", "bad_row", "corrupt_gzip", "missing"])
def test_ar_damage_never_advances_position(tmp_path, damage):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"id":1}\n')
    position = {}
    _incremental_jsonl_rows(path, position)
    before = deepcopy(position)
    if damage == "truncate":
        path.write_bytes(b'')
    elif damage == "replace":
        path.write_bytes(b'{"id":9}\n')
    elif damage == "bad_row":
        path.write_bytes(b'{"id":1}\nBAD\n')
    elif damage == "missing":
        path.unlink()
    else:
        path = tmp_path / "events.jsonl.gz"
        path.write_bytes(b'not gzip')
    with pytest.raises(ArchiveRepresentationError):
        _incremental_jsonl_rows(path, position)
    assert position == before


def test_ar_legacy_without_prefix_is_unknown_not_new_tail(tmp_path):
    path = tmp_path / "events.jsonl.gz"
    path.write_bytes(gzip.compress(b'{"id":1}\n'))
    cursor = ShadowCursor(tmp_path / "cursor.json", sources={str(path.with_suffix("").resolve()): {"offset": 9, "line": 1}})
    before = deepcopy(cursor.sources)
    with pytest.raises(ArchiveRepresentationError, match="UNKNOWN_ARCHIVE_PREFIX"):
        _incremental_jsonl_rows(path, cursor.position(path))
    assert cursor.sources == before


def test_ar_concurrent_change_and_false_line_count_are_rejected(tmp_path, monkeypatch):
    from poly_weather import archive_position

    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"id":1}\n')
    position = {}
    _incremental_jsonl_rows(path, position)
    before = deepcopy(position)
    real_identity = archive_position._identity
    calls = 0

    def racing_identity(source):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_bytes(b'{"id":1}\n{"id":2}\n')
        return real_identity(source)

    monkeypatch.setattr(archive_position, "_identity", racing_identity)
    with pytest.raises(ArchiveRepresentationError, match="CHANGED_DURING_READ"):
        _incremental_jsonl_rows(path, position)
    assert position == before
    monkeypatch.setattr(archive_position, "_identity", real_identity)
    position["line"] = 99
    with pytest.raises(ArchiveRepresentationError, match="BOUNDARY_MISMATCH"):
        _incremental_jsonl_rows(path, position)
