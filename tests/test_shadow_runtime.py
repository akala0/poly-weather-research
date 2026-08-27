import json

from poly_weather.shadow_runtime import run_shadow_spread_once


def test_shadow_runtime_requires_supervision_and_persists_status(tmp_path) -> None:
    try:
        run_shadow_spread_once(
            data_dir=tmp_path,
            ledger_path=tmp_path / "raw" / "shadow_orders.jsonl",
            status_path=tmp_path / "runtime" / "status.json",
            supervised=False,
        )
    except ValueError as exc:
        assert "supervised" in str(exc)
    else:
        raise AssertionError("unsupervised shadow runtime must fail closed")
    status_path = tmp_path / "runtime" / "status.json"
    status = run_shadow_spread_once(
        data_dir=tmp_path,
        ledger_path=tmp_path / "raw" / "shadow_orders.jsonl",
        status_path=status_path,
        supervised=True,
    )
    assert status["execution_enabled"] is False
    assert status["restart_idempotent"] is True
    assert "average_inventory_cost" in status
    assert status["unrealized_pnl_usd"] is None
    assert json.loads(status_path.read_text(encoding="utf-8"))["supervised"] is True
