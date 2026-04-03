from typer.testing import CliRunner

from web_listening.cli import app


runner = CliRunner()


def test_add_site_rejects_invalid_fetch_config_json():
    result = runner.invoke(
        app,
        [
            "add-site",
            "https://example.com",
            "--fetch-config",
            '{"broken":',
        ],
    )

    assert result.exit_code != 0
    assert "Invalid JSON for --fetch-config" in result.output
