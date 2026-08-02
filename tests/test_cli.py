from pathlib import Path

import pytest

from bc import __version__, cli
from bc.app import AppConfig
from bc.backends import BackendError, BackendErrorKind
from bc.cli import build_parser, main
from bc.config import AppSettings, load_sources_config
from bc.core import S3Location, parse_location

RUNNER_EXIT_CODE = 23


@pytest.fixture(autouse=True)
def stub_bucket_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep first-run bucket discovery away from real AWS in CLI tests."""

    monkeypatch.setattr(cli, "_discover_buckets", lambda _settings: ())


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


def test_cli_first_run_discovers_buckets_and_writes_sources_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def discover(settings: AppSettings) -> tuple[str, ...]:
        assert isinstance(settings, AppSettings)
        return ("beta", "alpha")

    monkeypatch.setattr(cli, "_discover_buckets", discover)

    def runner(config: AppConfig) -> int:
        assert [source.name for source in config.sources.sources] == ["alpha", "beta"]
        assert all(isinstance(source.location, S3Location) for source in config.sources.sources)
        return 0

    assert main([], app_runner=runner) == 0

    saved_path = tmp_path / "bucket-commander" / "sources.toml"
    reloaded = load_sources_config(saved_path)
    assert [source.name for source in reloaded.sources] == ["alpha", "beta"]
    assert reloaded.sources[0].location.uri == "s3://alpha/"


def test_cli_first_run_discovery_failure_keeps_sources_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def discover(_settings: AppSettings) -> tuple[str, ...]:
        raise BackendError(BackendErrorKind.IO_ERROR, "no credentials")

    monkeypatch.setattr(cli, "_discover_buckets", discover)

    def runner(config: AppConfig) -> int:
        assert config.sources.sources == ()
        assert config.startup_notice == "Bucket discovery failed: no credentials"
        return 0

    assert main([], app_runner=runner) == 0
    assert not (tmp_path / "bucket-commander" / "sources.toml").exists()


def test_cli_skips_discovery_when_sources_config_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(
        """
[[sources]]
name = "Existing"
uri = "s3://existing/"
""",
        encoding="utf-8",
    )

    def discover(_settings: AppSettings) -> tuple[str, ...]:
        pytest.fail("Discovery must not run when a sources config exists")

    monkeypatch.setattr(cli, "_discover_buckets", discover)

    def runner(config: AppConfig) -> int:
        assert [source.name for source in config.sources.sources] == ["Existing"]
        return 0

    assert main(["--sources-config", str(config_path)], app_runner=runner) == 0


def test_cli_forced_discovery_merges_new_buckets_into_existing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(
        """
[[sources]]
name = "existing"
uri = "s3://existing/"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_discover_buckets", lambda _settings: ("existing", "fresh"))

    def runner(config: AppConfig) -> int:
        assert [source.name for source in config.sources.sources] == ["existing", "fresh"]
        return 0

    args = ["--sources-config", str(config_path), "--discover-buckets"]
    assert main(args, app_runner=runner) == 0

    reloaded = load_sources_config(config_path)
    assert [source.name for source in reloaded.sources] == ["existing", "fresh"]


def test_cli_forced_discovery_without_new_buckets_keeps_config_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "sources.toml"
    original = """
[[sources]]
name = "existing"
uri = "s3://existing/"
"""
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli, "_discover_buckets", lambda _settings: ("existing",))

    def runner(config: AppConfig) -> int:
        assert [source.name for source in config.sources.sources] == ["existing"]
        return 0

    args = ["--sources-config", str(config_path), "--discover-buckets"]
    assert main(args, app_runner=runner) == 0
    assert config_path.read_text(encoding="utf-8") == original


def test_cli_forced_discovery_failure_keeps_existing_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(
        """
[[sources]]
name = "existing"
uri = "s3://existing/"
""",
        encoding="utf-8",
    )

    def discover(_settings: AppSettings) -> tuple[str, ...]:
        raise BackendError(BackendErrorKind.IO_ERROR, "no credentials")

    monkeypatch.setattr(cli, "_discover_buckets", discover)

    def runner(config: AppConfig) -> int:
        assert [source.name for source in config.sources.sources] == ["existing"]
        assert config.startup_notice == "Bucket discovery failed: no credentials"
        return 0

    args = ["--sources-config", str(config_path), "--discover-buckets"]
    assert main(args, app_runner=runner) == 0
    assert "Bucket discovery failed" in capsys.readouterr().err


def test_cli_version_action(capsys) -> None:  # type: ignore[no-untyped-def]
    parser = build_parser()

    try:
        parser.parse_args(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()

    assert "bc" in captured.out
