import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from poly_weather.cli import app


def test_cli_can_render_help() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "forecast-buckets" in result.stdout
    assert "evaluate-bucket-skill" in result.stdout
    assert "analyze-market-state-challenger" in result.stdout


def test_shadow_runtime_requires_supervised_flag_and_is_read_only(tmp_path) -> None:
    runner = CliRunner()
    rejected = runner.invoke(
        app,
        [
            "shadow-spread-engine",
            "--data-dir",
            str(tmp_path),
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--status",
            str(tmp_path / "status.json"),
        ],
    )
    assert rejected.exit_code != 0
    accepted = runner.invoke(
        app,
        [
            "shadow-spread-engine",
            "--supervised",
            "--data-dir",
            str(tmp_path),
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--status",
            str(tmp_path / "status.json"),
            "--once",
        ],
    )
    assert accepted.exit_code == 0
    assert '"execution_enabled": false' in accepted.stdout

    continuous = runner.invoke(
        app,
        [
            "shadow-spread-engine",
            "--supervised",
            "--data-dir",
            str(tmp_path),
            "--ledger",
            str(tmp_path / "continuous-ledger.jsonl"),
            "--status",
            str(tmp_path / "continuous-status.json"),
            "--cursor",
            str(tmp_path / "continuous-cursor.json"),
            "--runtime",
            "0.01",
        ],
    )
    assert continuous.exit_code == 0
    assert '"read_only_shadow_continuous"' in continuous.stdout


def test_stream_status_surfaces_only_token_scoped_shadow_status(tmp_path) -> None:
    from poly_weather.runtime_safety import atomic_json_write

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    updated_at = datetime.now(UTC).isoformat()
    atomic_json_write(
        runtime / "shadow_spread_status.json",
        {"schema_version": 1, "updated_at": updated_at, "state": "legacy"},
    )
    atomic_json_write(
        runtime / "shadow_spread_status_v2_token_scoped.json",
        {"schema_version": 2, "updated_at": updated_at, "state": "healthy"},
    )

    result = CliRunner().invoke(app, ["stream-status", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["shadow"]["schema_version"] == 2
    assert payload["shadow"]["reported_state"] == "healthy"
    assert payload["shadow"]["state"] == "stale"
    assert payload["shadow"]["pid_state"] == "missing"
    assert payload["shadow"]["health_ready"] is False
    assert payload["shadow"]["legacy_status"] == "superseded_v1_read_only"
    assert payload["shadow"]["legacy_status_path"].endswith(
        "shadow_spread_status_v1_legacy_read_only.json"
    )
