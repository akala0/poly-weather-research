from typer.testing import CliRunner

from poly_weather.cli import app


def test_cli_can_render_help() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "forecast-buckets" in result.stdout

