"""Snapshot identity, atomicity, freshness, resource lookup and CLI tests."""

import json
import sqlite3
from unittest.mock import Mock, patch

import pytest

from icloudpd.cli import cache_configuration_error, parse
from icloudpd.listing_cache import ListingSnapshot, SnapshotPhoto, cache_path, prepare_snapshot
from pyicloud_ipd.services.photos import PhotoAsset


def photo(asset="A1", master="M1", date=1000):
    resource = {"size": 4, "fileChecksum": "YQ==", "downloadURL": "https://signed.invalid/secret"}
    return PhotoAsset(
        {"recordName": master, "fields": {"resOriginalRes": {"value": resource}}},
        {
            "recordName": asset,
            "fields": {
                "masterRef": {"value": {"recordName": master}},
                "assetDate": {"value": date},
            },
        },
    )


class Album:
    def __init__(self, photos):
        self.items = photos
        self._zone_id = {"zoneName": "PrimarySync"}
        self.list_type = "list"
        self.obj_type = "objects"
        self.query_filter = None
        self.service_endpoint = "https://api.invalid"
        self.params = {"clientId": "test"}
        self.session = Mock()

    def __iter__(self):
        return iter(self.items)


def test_shared_master_asset_identity_and_private_cache(tmp_path):
    album = Album([photo(), photo("A2")])
    snapshot = prepare_snapshot(str(tmp_path), "user", "com", album, 2, 3600, False)
    assert [p._asset_record["recordName"] for p in snapshot.photos()] == ["A1", "A2"]
    assert snapshot.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(snapshot.path) as db:
        assert db.execute("SELECT count(*) FROM masters").fetchone()[0] == 1
        assert "signed.invalid" not in db.execute("SELECT payload FROM masters").fetchone()[0]
    assert snapshot.valid(2, 3600)
    assert not snapshot.valid(3, 3600)
    assert not snapshot.valid(2, 0)
    album.items = []
    assert (
        len(list(prepare_snapshot(str(tmp_path), "user", "com", album, 2, 3600, False).photos()))
        == 2
    )


def test_incomplete_or_duplicate_build_does_not_replace_snapshot(tmp_path):
    album = Album([photo()])
    snapshot = ListingSnapshot(tmp_path / "snapshot.sqlite3", album)
    snapshot.build(1)
    original = snapshot.path.read_bytes()
    with pytest.raises(ValueError, match="Incomplete"):
        snapshot.build(2)
    assert snapshot.path.read_bytes() == original
    album.items = [photo(), photo()]
    with pytest.raises(sqlite3.IntegrityError):
        snapshot.build(2)
    assert snapshot.path.read_bytes() == original
    assert not list(tmp_path.glob(".listing-*"))


def test_corrupt_and_scoped_cache(tmp_path):
    album = Album([])
    path = cache_path(tmp_path, "one", "com", album)
    assert path != cache_path(tmp_path, "two", "com", album)
    assert path != cache_path(tmp_path, "one", "cn", album)
    path.write_text("not sqlite")
    assert not ListingSnapshot(path, album).valid(0, 3600)
    assert not ListingSnapshot(tmp_path / "missing", album).valid(0, 3600)
    ListingSnapshot(path, album).build(0)
    assert ListingSnapshot(path, album).valid(0, 3600)


def test_lazy_lookup_and_changed_resource(tmp_path):
    album = Album([photo()])
    snap = ListingSnapshot(tmp_path / "cache", album)
    snap.build(1)
    cached = next(snap.photos())
    assert isinstance(cached, SnapshotPhoto)
    assert not album.session.post.called
    handle = next(iter(cached._resources))
    album.session.post.return_value.json.return_value = {"records": [photo()._master_record]}
    with patch("icloudpd.listing_cache.download_asset", return_value="ok") as download:
        assert cached.download(album.session, handle, 7) == "ok"
        download.assert_called_once_with(album.session, "https://signed.invalid/secret", 7)
    request = json.loads(album.session.post.call_args.kwargs["data"])
    assert request["records"] == [{"recordName": "M1"}]
    assert request["zoneID"] == album._zone_id
    with pytest.raises(ValueError, match="not part"):
        cached.download(album.session, "https://arbitrary.invalid")
    changed = next(snap.photos())
    fresh = photo()._master_record
    fresh["fields"]["resOriginalRes"]["value"]["size"] = 99
    album.session.post.return_value.json.return_value = {"records": [fresh]}
    with pytest.raises(ValueError, match="changed"):
        changed.download(album.session, next(iter(changed._resources)))
    missing = next(snap.photos())
    album.session.post.return_value.json.return_value = {"records": []}
    with pytest.raises(ValueError, match="unavailable"):
        missing.download(album.session, next(iter(missing._resources)))


def test_cache_cli_options():
    _, configs = parse(
        [
            "--username",
            "test",
            "--directory",
            "/tmp/photos",
            "--listing-cache-directory",
            "/tmp/cache",
            "--listing-cache-max-age",
            "30",
            "--refresh-listing-cache",
        ]
    )
    assert configs[0].listing_cache_directory == "/tmp/cache"
    assert configs[0].listing_cache_max_age == 30
    assert configs[0].refresh_listing_cache


@pytest.mark.parametrize(
    "option",
    ["--auto-delete", "--delete-after-download", "--prune-orphans", "--keep-icloud-recent-days"],
)
def test_cache_rejects_destructive_options(option):
    args = [
        "--username",
        "test",
        "--directory",
        "/tmp/photos",
        "--listing-cache-directory",
        "/tmp/cache",
        option,
    ]
    if option == "--keep-icloud-recent-days":
        args.append("0")
    _, configs = parse(args)
    assert cache_configuration_error(configs[0])


def test_cache_configuration_validation():
    _, configs = parse(["--username", "test", "--refresh-listing-cache"])
    assert cache_configuration_error(configs[0])
    configs[0].refresh_listing_cache = False
    configs[0].listing_cache_max_age = -1
    assert cache_configuration_error(configs[0])
    configs[0].listing_cache_max_age = 0
    assert cache_configuration_error(configs[0]) is None
