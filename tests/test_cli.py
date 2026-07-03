from pathlib import Path

from bc import __version__
from bc.app import AppConfig
from bc.cli import build_parser, main
from bc.core import parse_location

RUNNER_EXIT_CODE = 23


def test_package_exports_version() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_cli_runs_app_with_default_paths() -> None:
    captured_config: AppConfig | None = None

    def runner(config: AppConfig) -> int:
        nonlocal captured_config
        captured_config = config
        return RUNNER_EXIT_CODE

    exit_code = main([], app_runner=runner)

    assert exit_code == RUNNER_EXIT_CODE
    assert captured_config == AppConfig(
        left=parse_location(Path.cwd()),
        right=parse_location(Path.cwd()),
    )


def test_cli_accepts_initial_panel_paths(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"

    def runner(config: AppConfig) -> int:
        assert config == AppConfig(left=parse_location(left), right=parse_location(right))
        return 0

    assert main(["--left", str(left), "--right", str(right)], app_runner=runner) == 0


def test_cli_accepts_initial_s3_locations() -> None:
    def runner(config: AppConfig) -> int:
        assert config.left == parse_location("s3://example-bucket/logs/")
        assert config.right == parse_location(Path.cwd())
        return 0

    assert main(["--left", "s3://example-bucket/logs/"], app_runner=runner) == 0


def test_cli_version_action(capsys) -> None:  # type: ignore[no-untyped-def]
    parser = build_parser()

    try:
        parser.parse_args(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()

    assert "bucket-commander" in captured.out
