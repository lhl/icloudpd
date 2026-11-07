"""Transactional reconciliation of unfiltered All Photos metadata."""

import json
import sqlite3
from typing import Any

from icloudpd.listing_changes import RebuildRequired, changes, lookup
from pyicloud_ipd.services.photos import PhotoAlbum, PhotoAsset


def incremental_album(album: PhotoAlbum) -> bool:
    return (
        album.list_type == "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
        and not album.query_filter
    )


def relevant(record: dict[str, Any]) -> bool:
    # Untyped tombstones cannot safely be dismissed as unrelated.
    return record.get("recordType") in (
        "",
        None,
        "CPLAsset",
        "CPLMaster",
        "CPLAlbum",
        "CPLContainerRelation",
    )


def apply_changes(
    db: sqlite3.Connection, album: PhotoAlbum, token: str, sanitize
) -> tuple[str, int]:
    latest: dict[str, dict[str, Any]] = {}
    final_token = token
    for records, next_token in changes(album, token):
        final_token = next_token
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("recordName"), str):
                raise ValueError("Malformed changed record")
            if relevant(record):
                latest[record["recordName"]] = record
    if not latest:
        return final_token, 0
    if not incremental_album(album):
        raise RebuildRequired("Album changes require a full membership listing")

    changed_assets = set()
    changed_masters = set()
    for name, record in latest.items():
        kind = record.get("recordType")
        if record.get("deleted"):
            if (
                kind == "CPLAsset"
                or db.execute("SELECT 1 FROM assets WHERE id=?", (name,)).fetchone()
            ):
                db.execute("DELETE FROM assets WHERE id=?", (name,))
            elif (
                kind == "CPLMaster"
                or db.execute("SELECT 1 FROM masters WHERE id=?", (name,)).fetchone()
            ):
                # A master tombstone without all asset tombstones is ambiguous.
                raise RebuildRequired("Master deletion requires a full listing")
            elif kind not in ("CPLAlbum", "CPLContainerRelation"):
                raise RebuildRequired("Unknown tombstone requires a full listing")
        elif kind == "CPLAsset":
            changed_assets.add(name)
        elif kind == "CPLMaster":
            changed_masters.add(name)
        elif kind in (None, ""):
            raise RebuildRequired("Untyped changed record requires a full listing")

    # Fetch complete records; delta fields can be partial and removed fields
    # must not survive a merge with an old payload.
    fetched = lookup(album, changed_assets | changed_masters)
    for name in changed_assets:
        asset = fetched[name]
        if asset["recordType"] != "CPLAsset":
            raise RebuildRequired("Changed asset record type did not match")
        fields = asset["fields"]
        if any(
            fields.get(flag, {}).get("value") for flag in ("isDeleted", "isExpunged", "isHidden")
        ):
            db.execute("DELETE FROM assets WHERE id=?", (name,))
            continue
        master_id = fields.get("masterRef", {}).get("value", {}).get("recordName")
        if not isinstance(master_id, str):
            raise RebuildRequired("Changed asset has no master reference")
        if (
            master_id not in fetched
            and not db.execute("SELECT 1 FROM masters WHERE id=?", (master_id,)).fetchone()
        ):
            fetched.update(lookup(album, {master_id}))
        if master_id in fetched:
            master = fetched[master_id]
            if master.get("recordType") != "CPLMaster":
                raise RebuildRequired("Changed master record type did not match")
            db.execute(
                "INSERT OR REPLACE INTO masters VALUES (?, ?)",
                (master_id, json.dumps(sanitize(master))),
            )
        rank = db.execute("SELECT coalesce(min(rank), 0)-1 FROM assets").fetchone()[0]
        item = PhotoAsset({}, asset)
        created = item.asset_date.timestamp() if item.has_asset_date else None
        db.execute(
            "INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?, ?)",
            (name, rank, created, master_id, json.dumps(sanitize(asset))),
        )
    for name in changed_masters:
        master = fetched[name]
        if master.get("recordType") != "CPLMaster":
            raise RebuildRequired("Changed master record type did not match")
        db.execute(
            "INSERT OR REPLACE INTO masters VALUES (?, ?)", (name, json.dumps(sanitize(master)))
        )

    # Re-rank the unfiltered date index after changed dates and old-date imports.
    rows = db.execute("SELECT id FROM assets ORDER BY created IS NULL, created DESC, id").fetchall()
    previous = db.execute("SELECT min(rank), max(rank) FROM assets").fetchone()
    lower = int(previous[0] or 0)
    upper = int(previous[1] or 0)
    shift = upper - lower + len(rows) + 1
    db.execute("UPDATE assets SET rank=rank-?", (shift + max(upper, 0),))
    for rank, (name,) in enumerate(rows):
        db.execute("UPDATE assets SET rank=? WHERE id=?", (rank, name))
    db.execute("DELETE FROM masters WHERE id NOT IN (SELECT master_id FROM assets)")
    return final_token, len(latest)
