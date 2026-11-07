"""Change-token updates must be all-or-nothing and keyed by asset, not master."""

import copy
import sqlite3

import pytest

from icloudpd.listing_cache import ListingSnapshot, prepare_snapshot
from icloudpd.listing_changes import RebuildRequired, changes
from tests.test_listing_cache import Album, photo


def record(item, kind):
    value = copy.deepcopy(item._asset_record if kind == "CPLAsset" else item._master_record)
    value["recordType"] = kind
    return value


def page(records=(), token="t1", more=False):
    return {
        "zones": [
            {
                "zoneID": {"zoneName": "PrimarySync"},
                "syncToken": token,
                "records": list(records),
                "moreComing": more,
            }
        ]
    }


def setup_snapshot(tmp_path, items):
    album = Album(items)
    album.list_type = "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
    snapshot = ListingSnapshot(tmp_path / "cache", album)
    snapshot.build(len(items), track_changes=True)
    return album, snapshot


def meta(snapshot):
    with sqlite3.connect(snapshot.path) as db:
        return dict(db.execute("SELECT * FROM metadata"))


def test_old_date_import_and_shared_master_edit_delete(tmp_path):
    original = photo("A1", date=3000)
    album, snap = setup_snapshot(tmp_path, [original, photo("A2", date=2000)])
    imported = photo("A3", date=500)
    edited = photo("A2", date=1000)
    album.items = [edited, imported]
    album.session.post.return_value.json.side_effect = [
        page(
            [
                {"recordName": "A1", "recordType": "CPLAsset", "deleted": True},
                {"recordName": "A2", "recordType": "CPLAsset"},
            ],
            "t1",
            True,
        ),
        page([{"recordName": "A3", "recordType": "CPLAsset"}], "t2"),
        {"records": [record(edited, "CPLAsset"), record(imported, "CPLAsset")]},
    ]
    assert snap.synchronize() == 3
    result = list(snap.photos())
    assert {p._asset_record["recordName"] for p in result} == {"A2", "A3"}
    assert [p.asset_date.timestamp() for p in result] == [1, 0.5]
    assert meta(snap)["sync_token"] == "t2"
    assert len({p.id for p in result}) == 1


def test_partial_page_failure_rolls_back_records_and_token(tmp_path):
    album, snap = setup_snapshot(tmp_path, [photo()])
    before = snap.path.read_bytes()
    album.session.post.return_value.json.side_effect = [
        page([], "t1", True),
        RuntimeError("network"),
    ]
    with pytest.raises(RuntimeError, match="network"):
        snap.synchronize()
    assert snap.path.read_bytes() == before


def test_count_mismatch_rolls_back_and_same_count_edit_detected(tmp_path):
    album, snap = setup_snapshot(tmp_path, [photo()])
    before = snap.path.read_bytes()
    album.session.post.return_value.json.side_effect = [
        page([{"recordName": "A1", "recordType": "CPLAsset", "deleted": True}])
    ]
    with pytest.raises(RebuildRequired, match="count"):
        snap.synchronize()
    assert snap.path.read_bytes() == before
    edited = photo(date=9999)
    album.session.post.return_value.json.side_effect = [
        page([{"recordName": "A1", "recordType": "CPLAsset"}]),
        {"records": [record(edited, "CPLAsset")]},
    ]
    snap.synchronize()
    assert next(snap.photos()).asset_date.timestamp() == 9.999


def test_missing_new_master_lookup_and_removed_resource_fields(tmp_path):
    album, snap = setup_snapshot(tmp_path, [])
    new = photo("A2", "M2")
    album.items = [new]
    album.session.post.return_value.json.side_effect = [
        page([{"recordName": "A2", "recordType": "CPLAsset"}]),
        {"records": [record(new, "CPLAsset")]},
        {"records": [record(new, "CPLMaster")]},
    ]
    snap.synchronize()
    assert next(snap.photos()).id == "M2"
    fresh = record(new, "CPLMaster")
    fresh["fields"] = {}
    album.session.post.return_value.json.side_effect = [
        page([{"recordName": "M2", "recordType": "CPLMaster"}]),
        {"records": [fresh]},
    ]
    snap.synchronize()
    assert next(snap.photos())._master_record["fields"] == {}


def test_soft_delete_does_not_remove_shared_master_neighbor(tmp_path):
    album, snap = setup_snapshot(tmp_path, [photo("A1"), photo("A2")])
    hidden = record(photo("A1"), "CPLAsset")
    hidden["fields"]["isHidden"] = {"value": 1}
    album.items = [photo("A2")]
    album.session.post.return_value.json.side_effect = [
        page([{"recordName": "A1", "recordType": "CPLAsset"}]),
        {"records": [hidden]},
    ]
    snap.synchronize()
    assert [p._asset_record["recordName"] for p in snap.photos()] == ["A2"]


def test_initial_build_mutation_never_published(tmp_path):
    album = Album([photo()])
    snap = ListingSnapshot(tmp_path / "cache", album)
    snap.build(1)
    before = snap.path.read_bytes()
    album.session.post.return_value.json.side_effect = [
        {"syncToken": "anchor"},
        page([{"recordName": "A2", "recordType": "CPLAsset"}]),
    ]
    with pytest.raises(RebuildRequired, match="during enumeration"):
        snap.build(1, track_changes=True)
    assert snap.path.read_bytes() == before


def test_album_membership_change_requires_rebuild(tmp_path):
    album, snap = setup_snapshot(tmp_path, [photo()])
    album.query_filter = [{"fieldName": "parentId"}]
    album.session.post.return_value.json.side_effect = [
        page([{"recordName": "relation", "recordType": "CPLContainerRelation"}])
    ]
    with pytest.raises(RebuildRequired, match="membership"):
        snap.synchronize()


def test_token_expiry_rebuild_and_no_network_fallback_on_transport_error(tmp_path):
    album = Album([photo()])
    album.list_type = "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
    prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False)
    album.session.post.return_value.json.side_effect = [
        {"serverErrorCode": "CHANGE_TOKEN_EXPIRED"},
        {"syncToken": "new-anchor"},
        page([], "fresh"),
    ]
    rebuilt = prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False)
    assert meta(rebuilt)["sync_token"] == "fresh"
    album.session.post.return_value.json.side_effect = RuntimeError("offline")
    before = rebuilt.path.read_bytes()
    with pytest.raises(RuntimeError, match="offline"):
        prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False)
    assert rebuilt.path.read_bytes() == before


@pytest.mark.parametrize(
    "payload", [{"zones": []}, page([], "t0", True), {"zones": [{"zoneID": {"zoneName": "wrong"}}]}]
)
def test_bad_change_responses_fail_closed(payload):
    album = Album([])
    album.session.post.return_value.json.return_value = payload
    with pytest.raises(ValueError):
        list(changes(album, "t0"))


def test_missing_token_and_corrupt_response_do_not_publish(tmp_path):
    album = Album([photo()])
    snap = ListingSnapshot(tmp_path / "cache", album)
    album.session.post.return_value.json.return_value = {"records": []}
    with pytest.raises(ValueError, match="change token"):
        snap.build(1, track_changes=True)
    assert not snap.path.exists()
    album.session.post.return_value.json.return_value = []
    with pytest.raises(ValueError, match="Invalid CloudKit"):
        snap.build(1, track_changes=True)


def test_unknown_tombstone_missing_lookup_and_wrong_types_roll_back(tmp_path):
    album, snap = setup_snapshot(tmp_path, [photo()])
    before = snap.path.read_bytes()
    responses = [
        [page([{"recordName": "unknown", "deleted": True}])],
        [page([{"recordName": "M1", "recordType": "CPLMaster", "deleted": True}])],
        [page([{"recordName": "A1", "recordType": "CPLAsset"}]), {"records": []}],
        [
            page([{"recordName": "A1", "recordType": "CPLAsset"}]),
            {"records": [record(photo("A1"), "CPLMaster")]},
        ],
    ]
    for response in responses:
        album.session.post.return_value.json.side_effect = response
        with pytest.raises(RebuildRequired):
            snap.synchronize()
        assert snap.path.read_bytes() == before


def test_auto_retries_unstable_initial_scan_once(tmp_path):
    album = Album([photo()])
    album.session.post.return_value.json.side_effect = [
        {"syncToken": "a"},
        page([{"recordName": "other", "recordType": "CPLAsset"}]),
        {"syncToken": "b"},
        page([], "c"),
    ]
    snap = prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False)
    assert meta(snap)["sync_token"] == "c"
    before = snap.path.read_bytes()
    album.session.post.return_value.json.side_effect = [
        {"syncToken": "a"},
        page([{"recordName": "other", "recordType": "CPLAsset"}]),
        {"syncToken": "b"},
        page([{"recordName": "other", "recordType": "CPLAsset"}]),
    ]
    with pytest.raises(RebuildRequired):
        prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, True)
    assert snap.path.read_bytes() == before


def test_modes_and_missing_token_rebuild(tmp_path):
    album = Album([photo()])
    with pytest.raises(ValueError, match="Unknown"):
        prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False, "bad")
    with pytest.raises(ValueError, match="No matching"):
        prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False, "reuse")
    snap = prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False, "full")
    with sqlite3.connect(snap.path) as db:
        db.execute("DELETE FROM metadata WHERE key='sync_token'")
    with pytest.raises(RebuildRequired, match="no change token"):
        snap.synchronize()
    snap = prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False)
    assert meta(snap)["sync_token"]


def test_hidden_asset_unhide_and_full_master_fields(tmp_path):
    album, snap = setup_snapshot(tmp_path, [])
    item = photo("unhidden", "master")
    asset = record(item, "CPLAsset")
    asset["fields"]["isHidden"] = {"value": 0}
    album.items = [item]
    album.session.post.return_value.json.side_effect = [
        page(
            [
                {"recordName": "unhidden", "recordType": "CPLAsset"},
                {"recordName": "master", "recordType": "CPLMaster"},
            ]
        ),
        {"records": [asset, record(item, "CPLMaster")]},
    ]
    snap.synchronize()
    assert next(snap.photos())._asset_record["recordName"] == "unhidden"


def test_change_feed_never_advances_after_malformed_record(tmp_path):
    album, snap = setup_snapshot(tmp_path, [photo()])
    before = snap.path.read_bytes()
    album.session.post.return_value.json.side_effect = [page([{"recordType": "CPLAsset"}])]
    with pytest.raises(ValueError, match="Malformed"):
        snap.synchronize()
    assert snap.path.read_bytes() == before


def test_feed_error_types_and_malformed_page():
    from icloudpd.listing_changes import request
    from pyicloud_ipd.exceptions import PyiCloudAPIResponseException

    album = Album([])
    album.session.post.side_effect = PyiCloudAPIResponseException("expired", "CHANGE_TOKEN_EXPIRED")
    with pytest.raises(RebuildRequired):
        request(album, "changes/zone", {})
    album.session.post.side_effect = PyiCloudAPIResponseException("denied", "ACCESS_DENIED")
    with pytest.raises(PyiCloudAPIResponseException):
        request(album, "changes/zone", {})
    album.session.post.side_effect = None
    album.session.post.return_value.json.return_value = {"serverErrorCode": "OTHER_ERROR"}
    with pytest.raises(ValueError, match="OTHER_ERROR"):
        request(album, "changes/zone", {})
    album.session.post.return_value.json.return_value = page([])
    album.session.post.return_value.json.return_value["zones"][0]["records"] = {}
    with pytest.raises(ValueError, match="Malformed"):
        list(changes(album, "t0"))


def test_duplicate_during_tracked_build_and_corrupt_auto_cache(tmp_path):
    from icloudpd.listing_cache import cache_path

    album = Album([photo(), photo()])
    snap = ListingSnapshot(tmp_path / "dup", album)
    with pytest.raises(RebuildRequired, match="Duplicate"):
        snap.build(2, track_changes=True)
    assert not snap.path.exists()
    album.items = [photo()]
    path = cache_path(tmp_path, "user", "com", album)
    path.write_text("bad database")
    rebuilt = prepare_snapshot(str(tmp_path), "user", "com", album, 1, 3600, False)
    assert rebuilt.valid(1, 3600)


def test_reconciliation_rejects_untyped_and_missing_refs(tmp_path):
    album, snap = setup_snapshot(tmp_path, [photo()])
    asset = record(photo(), "CPLAsset")
    del asset["fields"]["masterRef"]
    for replies in (
        [page([{"recordName": "untyped"}])],
        [page([{"recordName": "A1", "recordType": "CPLAsset"}]), {"records": [asset]}],
    ):
        album.session.post.return_value.json.side_effect = replies
        with pytest.raises(RebuildRequired):
            snap.synchronize()
    album.session.post.return_value.json.side_effect = [
        page([{"recordName": "M1", "recordType": "CPLMaster"}]),
        {"records": [record(photo("M1"), "CPLAsset")]},
    ]
    with pytest.raises(RebuildRequired, match="master record type"):
        snap.synchronize()


def test_concurrent_cache_writer_refused(tmp_path):
    from icloudpd.listing_cache import cache_writer_lock

    path = tmp_path / "cache.sqlite3"
    with (
        cache_writer_lock(path),
        pytest.raises(sqlite3.OperationalError, match="locked"),
        cache_writer_lock(path),
    ):
        pytest.fail("Second writer acquired lock")
    with cache_writer_lock(path):
        pass


def test_irrelevant_zone_record_advances_token(tmp_path):
    album, snap = setup_snapshot(tmp_path, [photo()])
    album.session.post.return_value.json.side_effect = [
        page([{"recordName": "irrelevant", "recordType": "Other"}], "new")
    ]
    assert snap.synchronize() == 0
    assert meta(snap)["sync_token"] == "new"
