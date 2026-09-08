from typer.testing import CliRunner

from poly_weather.cli import app


def test_paper_engine_requires_explicit_strategy_config(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "paper-spread-engine",
            "--supervised",
            "--ledger",
            str(tmp_path / "paper.jsonl"),
            "--status",
            str(tmp_path / "status.json"),
        ],
    )
    assert result.exit_code != 0
    assert "--strategy-config" in result.output


def test_paper_status_uses_empty_isolated_ledger(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "paper-spread-status",
            "--ledger",
            str(tmp_path / "paper.jsonl"),
            "--strategy-config",
            "configs/paper_spread_strategy_v1.json",
        ],
    )
    assert result.exit_code == 0
    assert '"execution_enabled": false' in result.output
    assert '"paper_score_eligible": false' in result.output
    assert '"supervisor_evidence_unreadable"' in result.output
