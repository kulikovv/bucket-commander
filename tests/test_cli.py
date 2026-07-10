from pathlib import Path

from bc import __version__
from bc.app import AppConfig
from bc.cli import build_parser, main
from bc.core import S3Location, parse_location

RUNNER_EXIT_CODE = 23


def test_package_exports_version() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_cli_runs_app_with_default_paths(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
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


def test_cli_accepts_initial_panel_paths(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    left = tmp_path / "left"
    right = tmp_path / "right"

    def runner(config: AppConfig) -> int:
        assert config == AppConfig(left=parse_location(left), right=parse_location(right))
        return 0

    assert main(["--left", str(left), "--right", str(right)], app_runner=runner) == 0


def test_cli_accepts_initial_s3_locations(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    def runner(config: AppConfig) -> int:
        assert config.left == parse_location("s3://example-bucket/logs/")
        assert config.right == parse_location(Path.cwd())
        return 0

    assert main(["--left", "s3://example-bucket/logs/"], app_runner=runner) == 0


def test_cli_loads_sources_config(tmp_path: Path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(
        """
[[sources]]
name = "Local MinIO"
uri = "s3://bucket-commander/logs/"
endpoint_url = "http://127.0.0.1:9000"
""",
        encoding="utf-8",
    )

    def runner(config: AppConfig) -> int:
        assert config.sources.sources[0].name == "Local MinIO"
        assert isinstance(config.sources.sources[0].location, S3Location)
        assert config.sources.sources[0].location.endpoint_url == "http://127.0.0.1:9000"
        return 0

    assert main(["--sources-config", str(config_path)], app_runner=runner) == 0


def test_cli_loads_app_settings_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    cache_root = tmp_path / "cache"
    config_path.write_text(
        f"""
[cache]
root = "{cache_root}"

[s3]
profile = "dev"
region = "us-east-1"
endpoint_url = "http://127.0.0.1:9000/"
""",
        encoding="utf-8",
    )

    def runner(config: AppConfig) -> int:
        assert config.settings.cache_root == cache_root
        assert config.settings.profiles.default_s3.profile_name == "dev"
        assert config.settings.profiles.default_s3.region_name == "us-east-1"
        assert config.settings.profiles.default_s3.endpoint_url == "http://127.0.0.1:9000/"
        return 0

    assert main(["--config", str(config_path)], app_runner=runner) == 0


def test_cli_loads_project_sources_config_by_default(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "sources.toml").write_text(
        """
[[sources]]
name = "Local MinIO"
uri = "s3://bucket-commander/"
""",
        encoding="utf-8",
    )

    def runner(config: AppConfig) -> int:
        assert config.sources.sources[0].name == "Local MinIO"
        return 0

    assert main([], app_runner=runner) == 0


def test_cli_version_action(capsys) -> None:  # type: ignore[no-untyped-def]
    parser = build_parser()

    try:
        parser.parse_args(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()

    assert "bc" in captured.out
