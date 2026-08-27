from typer.testing import CliRunner

from poly_weather.cli import app


def test_cli_can_render_help() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "forecast-buckets" in result.stdout
    assert "evaluate-bucket-skill" in result.stdout


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
        ],
    )
    assert accepted.exit_code == 0
    assert '"execution_enabled": false' in accepted.stdout
