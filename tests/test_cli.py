from bc import __version__
from bc.cli import build_parser, main


def test_package_exports_version() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_cli_help_runs(capsys) -> None:  # type: ignore[no-untyped-def]
    exit_code = main([])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Two-panel terminal file manager" in captured.out


def test_cli_version_action(capsys) -> None:  # type: ignore[no-untyped-def]
    parser = build_parser()

    try:
        parser.parse_args(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()

    assert "bucket-commander" in captured.out

