import gzip

import pytest

from poly_weather.signal_engine import JsonlTail


def test_signal_tail_plain_gzip_partial_and_prefix_replacement(tmp_path):
    path = tmp_path / "events.jsonl"
    prefix = '{"text":"天气"}\n'.encode()
    path.write_bytes(prefix + b'{"second":')
    tail = JsonlTail(lambda: path)
    assert tail.poll() == [{"text": "天气"}]
    assert tail.offset == len(prefix)
    complete = prefix + b'{"second":2}\n'
    path.write_bytes(complete)
    packed = path.with_suffix(".jsonl.gz")
    packed.write_bytes(gzip.compress(complete))
    assert tail.poll() == [{"second": 2}]
    path.unlink()  # Only our temporary fixture's plain representation.
    assert tail.poll() == []
    committed = dict(tail.positions)
    packed.write_bytes(gzip.compress(complete.replace(b"second", b"tamper")))
    with pytest.raises(ValueError):
        tail.poll()
    assert tail.positions == committed
