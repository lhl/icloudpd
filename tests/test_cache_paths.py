"""Platform path selection is deterministic and needs no filesystem/network I/O."""

from pathlib import PurePosixPath, PureWindowsPath
from unittest.mock import patch

import pytest

from icloudpd.cache_paths import default_listing_cache_directory
from icloudpd.cli import cache_configuration_error, parse


@pytest.mark.parametrize(
    "xdg, expected",
    [
        (None, "/home/test/.cache/icloudpd/listings"),
        ("", "/home/test/.cache/icloudpd/listings"),
        ("relative", "/home/test/.cache/icloudpd/listings"),
        ("~/cache", "/home/test/.cache/icloudpd/listings"),
        ("/disk/Cache Space", "/disk/Cache Space/icloudpd/listings"),
    ],
)
def test_linux_cache_root(xdg, expected):
    env = {} if xdg is None else {"XDG_CACHE_HOME": xdg}
    assert (
        default_listing_cache_directory(
            platform="linux", environ=env, home=PurePosixPath("/home/test")
        )
        == expected
    )


def test_mac_uses_native_caches_not_xdg():
    assert (
        default_listing_cache_directory(
            platform="darwin",
            environ={"XDG_CACHE_HOME": "/ignored"},
            home=PurePosixPath("/Users/test"),
        )
        == "/Users/test/Library/Caches/icloudpd/listings"
    )


@pytest.mark.parametrize(
    "local, expected",
    [
        (None, r"C:\Users\test\AppData\Local\icloudpd\Cache\listings"),
        ("", r"C:\Users\test\AppData\Local\icloudpd\Cache\listings"),
        ("relative", r"C:\Users\test\AppData\Local\icloudpd\Cache\listings"),
        (r"C:relative", r"C:\Users\test\AppData\Local\icloudpd\Cache\listings"),
        (r"\root-relative", r"C:\Users\test\AppData\Local\icloudpd\Cache\listings"),
        (r"D:\Local Data", r"D:\Local Data\icloudpd\Cache\listings"),
        (r"\\server\share\Local", r"\\server\share\Local\icloudpd\Cache\listings"),
    ],
)
def test_windows_cache_root(local, expected):
    env = {} if local is None else {"LOCALAPPDATA": local}
    assert (
        default_listing_cache_directory(
            platform="win32", environ=env, home=PureWindowsPath(r"C:\Users\test")
        )
        == expected
    )


def test_runtime_defaults_do_not_create_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("icloudpd.cache_paths.sys.platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "not-created"))
    assert default_listing_cache_directory() == str(tmp_path / "not-created/icloudpd/listings")
    assert not (tmp_path / "not-created").exists()


def test_cache_cli_opt_in_and_explicit_override():
    with patch(
        "icloudpd.cli.default_listing_cache_directory", return_value="native-cache"
    ) as default:
        _, configs = parse(["--username", "test"])
        assert configs[0].listing_cache_directory is None
        default.assert_not_called()
        _, configs = parse(["--username", "test", "--cache", "--refresh"])
        assert configs[0].listing_cache_directory == "native-cache"
        assert cache_configuration_error(configs[0]) is None
        for options in (
            ["--cache", "--cache-directory", "custom"],
            ["--cache-directory", "custom", "--cache"],
            ["--listing-cache-directory", "custom"],
        ):
            _, configs = parse(["--username", "test", *options])
            assert configs[0].listing_cache_directory == "custom"


@pytest.mark.parametrize(
    "options",
    [
        ["--refresh"],
        ["--refresh-listing-cache"],
        ["--cache-refresh", "full"],
        ["--cache-refresh", "reuse"],
    ],
)
def test_refresh_options_enable_native_cache(options):
    with patch("icloudpd.cli.default_listing_cache_directory", return_value="native-cache"):
        _, configs = parse(["--username", "test", *options])
        assert configs[0].listing_cache_directory == "native-cache"
        assert cache_configuration_error(configs[0]) is None
        _, configs = parse(["--username", "test", *options, "--cache-directory", "custom"])
        assert configs[0].listing_cache_directory == "custom"


def test_implicit_refresh_cache_preserves_safety_guards():
    _, configs = parse(["--username", "test", "--refresh", "--auto-delete"])
    assert "deletion" in cache_configuration_error(configs[0])
    _, configs = parse(["--username", "test", "--refresh", "--cache-refresh", "reuse"])
    assert "cannot be combined" in cache_configuration_error(configs[0])
    _, configs = parse(["--username", "test", "--cache-refresh", "auto"])
    assert configs[0].listing_cache_directory is None


def test_cache_cli_multiple_user_defaults_and_override():
    with patch("icloudpd.cli.default_listing_cache_directory", return_value="native-cache"):
        _, configs = parse(
            ["--cache", "--username", "one", "--username", "two", "--cache-directory", "two-cache"]
        )
    assert [config.listing_cache_directory for config in configs] == ["native-cache", "two-cache"]
