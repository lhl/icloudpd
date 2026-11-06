from pathlib import Path

import pytest

from icloudpd.cli import cache_configuration_error, parse
from icloudpd.listing_cache import ListingSnapshot
from tests.test_listing_cache import Album, photo


def test_chronological_order_ties_unknowns_and_range(tmp_path: Path):
    album = Album(
        [
            photo("z", date=2000),
            photo("new", date=3000),
            photo("old", date=1000),
            photo("a", date=2000),
            photo("unknown", date=None),
        ]
    )
    snap = ListingSnapshot(tmp_path / "cache", album)
    snap.build(5)

    def ids(**kwargs):
        return [p._asset_record["recordName"] for p in snap.photos(**kwargs)]

    assert ids(date_order="ascending") == ["old", "a", "z", "new", "unknown"]
    assert ids(date_order="descending") == ["new", "a", "z", "old", "unknown"]
    assert ids() == ["z", "new", "old", "a", "unknown"]
    assert ids(created_after=2, date_order="ascending") == ["old", "a", "z", "unknown"]
    with pytest.raises(ValueError, match="Unknown"):
        ids(date_order="SQL injection")


def test_order_cli_and_early_stop_guards():
    _, configs = parse(["--username", "test", "--date-order", "ascending"])
    config = configs[0]
    assert cache_configuration_error(config)
    config.listing_cache_directory = "/tmp/cache"
    assert cache_configuration_error(config) is None
    config.recent = 2
    assert cache_configuration_error(config)
    config.recent = None
    config.until_found = 2
    assert cache_configuration_error(config)
    config.until_found = None
    config.date_order = "descending"
    assert cache_configuration_error(config) is None
