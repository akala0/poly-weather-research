"""P4 scoped shared-consumer tests; not whole-project qualification."""

import gzip
import json
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from test_market_trade_tape import _row
from test_paper_trade_evidence import public_match
from test_public_trade_collection import _FakeClient, _trade
from test_reliability_storage import coverage_fixture

from poly_weather.archive_io import ArchiveRepresentationError, jsonl_archive_paths, open_jsonl_text
from poly_weather.fees import trading_fee_usdc
from poly_weather.market_trade_tape import (
    build_shadow_trade_events,
    match_ws_trade,
    parse_market_ws_trade,
)
from poly_weather.polymarket_status import UpstreamQualityWindow
from poly_weather.public_trade_collection import collect_depth_event_trades
from poly_weather.receipt_journal import ReceiptIntegrityError
from poly_weather.shadow_orders import maker_fee_usdc, taker_fee_usdc
from poly_weather.shadow_runtime import _public_trade_events_from_file
from poly_weather.trade_tape_analysis import load_event_trade_tapes


@pytest.mark.parametrize("equal", [True, False])
def test_q01_duplicate_archive_representations_verified_or_blocked(tmp_path, equal):
    plain = tmp_path / "day" / "events.jsonl"
    plain.parent.mkdir()
    raw = b'{"value":1}\n'
    plain.write_bytes(raw)
    compressed = plain.with_suffix(".jsonl.gz")
    compressed.write_bytes(gzip.compress(raw if equal else b'{"value":2}\n'))
    before = plain.read_bytes(), compressed.read_bytes()
    if equal:
        assert jsonl_archive_paths(tmp_path) == [plain]
        with open_jsonl_text(plain) as handle:
            assert handle.read() == raw.decode()
    else:
        with pytest.raises(ArchiveRepresentationError):
            jsonl_archive_paths(tmp_path)
    assert (plain.read_bytes(), compressed.read_bytes()) == before


def test_q03_quality_interval_interior_rejected_by_all_converters(tmp_path):
    ws = parse_market_ws_trade(_row())
    source = ws.source_timestamp
    ws = replace(ws, received_at=source + timedelta(minutes=3))
    api = public_match(ws)
    incident = UpstreamQualityWindow(
        incident_id="fixture", title="interior gap", incident_type="incident",
        start_at=source + timedelta(minutes=1), end_at=source + timedelta(minutes=2),
        affected_components=(), affects_market_data=True, affects_trading=False,
        status="closed", source_url="fixture", default_excluded=True,
    )
    assert match_ws_trade(ws, [api], quality_windows=[incident])["reason"] == "UNKNOWN_QUALITY_WINDOW"
    assert build_shadow_trade_events([ws], [api], quality_windows=[incident])[0] == ()
    assert build_shadow_trade_events([], [api], quality_windows=[incident])[0] == ()
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"event_slug": "event", "trades": [api.as_json()]}))
    assert _public_trade_events_from_file(path, quality_windows=[incident]) == ()


def test_q03_durable_receipt_clock_and_corruption_shared_by_loaders(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    ticks = iter(at + timedelta(hours=n) for n in [1, 2, 3, 4])
    collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                              output_dir=root, clock=lambda: next(ticks))
    path = root / "event-1.json"
    assert load_event_trade_tapes(root)["event-1"][0].available_at == at + timedelta(hours=3)
    assert _public_trade_events_from_file(path)[0].available_at == at + timedelta(hours=3)
    payload = json.loads(path.read_text())
    payload["trades"][0]["available_at"] = at.isoformat()
    path.write_text(json.dumps(payload))
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    for reader in (lambda: load_event_trade_tapes(root), lambda: _public_trade_events_from_file(path)):
        with pytest.raises(ReceiptIntegrityError):
            reader()
    assert {p: p.read_bytes() for p in root.rglob("*.json")} == before


@pytest.mark.parametrize("price", ["0.01", "0.50", "0.82", "0.99"])
def test_q05_fixed_weather_fee_math_and_shared_wrappers(price):
    shares, native_price = Decimal("23.456"), Decimal(price)
    expected = (shares * Decimal("0.05") * native_price * (1 - native_price)).quantize(
        Decimal("0.00001"), rounding=ROUND_HALF_UP)
    assert taker_fee_usdc(shares, native_price) == trading_fee_usdc(shares, native_price) == expected
    assert maker_fee_usdc(shares, native_price) == 0
