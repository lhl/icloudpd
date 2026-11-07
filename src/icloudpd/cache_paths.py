"""Native locations for disposable application caches, not credentials or media."""

import os
import sys
from pathlib import Path, PurePath, PureWindowsPath
from typing import Mapping


def default_listing_cache_directory(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: PurePath | None = None,
) -> str:
    """Ignore unset, empty or relative cache roots; do not create directories."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    if platform == "win32":
        windows_home = PureWindowsPath(home)
        local = environ.get("LOCALAPPDATA", "")
        windows_root = (
            PureWindowsPath(local)
            if local and PureWindowsPath(local).is_absolute()
            else windows_home / "AppData" / "Local"
        )
        return str(windows_root / "icloudpd" / "Cache" / "listings")
    if platform == "darwin":
        return str(home / "Library" / "Caches" / "icloudpd" / "listings")
    xdg = environ.get("XDG_CACHE_HOME", "")
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".cache"
    return str(root / "icloudpd" / "listings")
