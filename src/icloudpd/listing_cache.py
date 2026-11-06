"""Opt-in, complete metadata snapshots; download URLs are resolved only when needed."""

import copy
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from pyicloud_ipd.services.photos import PhotoAlbum, PhotoAsset, download_asset

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def cache_path(directory: Path, account: str, domain: str, album: PhotoAlbum) -> Path:
    """Separate accounts, domains, zones, album queries and cache formats."""
    scope = [
        SCHEMA_VERSION,
        account.casefold(),
        domain,
        album._zone_id,
        album.list_type,
        album.obj_type,
        album.query_filter,
    ]
    key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    return directory.expanduser() / f"{key}.sqlite3"


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(record)
    for field in result.get("fields", {}).values():
        value = field.get("value") if isinstance(field, dict) else None
        if isinstance(value, dict) and "downloadURL" in value:
            # Signed media URLs expire and must not become persistent credentials.
            value["downloadURL"] = ""
    return result


class SnapshotPhoto(PhotoAsset):
    """A cached asset whose resource URLs are replaced by lazy lookup handles."""

    def __init__(self, master: dict[str, Any], asset: dict[str, Any], album: PhotoAlbum):
        master, asset = copy.deepcopy(master), copy.deepcopy(asset)
        self._album = album
        self._resources: dict[str, tuple[dict[str, Any], str]] = {}
        self._fresh_records: dict[str, dict[str, Any]] = {}
        for record in (master, asset):
            for key, field in record.get("fields", {}).items():
                value = field.get("value") if isinstance(field, dict) else None
                if isinstance(value, dict) and "downloadURL" in value:
                    handle = f"icloudpd-cache://resource/{quote(record['recordName'], safe='')}/{quote(key, safe='')}"
                    value["downloadURL"] = handle
                    self._resources[handle] = (record, key)
        super().__init__(master, asset)

    def download(self, session: Any, url: str, start: int = 0) -> Any:
        if url not in self._resources:
            raise ValueError("Cached resource is not part of this asset")
        record, key = self._resources[url]
        record_id = record["recordName"]
        if record_id not in self._fresh_records:
            endpoint = self._album.service_endpoint + "/records/lookup"
            response = self._album.session.post(
                endpoint,
                params=self._album.params,
                data=json.dumps(
                    {"records": [{"recordName": record_id}], "zoneID": self._album._zone_id}
                ),
                headers={"Content-type": "text/plain"},
            ).json()
            for fresh in response.get("records", []):
                if fresh.get("recordName") == record_id and "fields" in fresh:
                    self._fresh_records[record_id] = fresh
            if record_id not in self._fresh_records:
                raise ValueError("Cached asset is unavailable; refresh the listing cache")
        cached = record["fields"][key]["value"]
        fresh = self._fresh_records[record_id].get("fields", {}).get(key, {}).get("value", {})
        if (
            not fresh.get("downloadURL")
            or fresh.get("size") != cached.get("size")
            or fresh.get("fileChecksum") != cached.get("fileChecksum")
        ):
            raise ValueError("Cached resource changed; rerun with --refresh-listing-cache")
        return download_asset(session, fresh["downloadURL"], start)


class ListingSnapshot:
    def __init__(self, path: Path, album: PhotoAlbum):
        self.path = path
        self.album = album

    def valid(self, expected_count: int, max_age: int) -> bool:
        if not self.path.is_file():
            return False
        try:
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as db:
                meta = dict(db.execute("SELECT key, value FROM metadata"))
                age = time.time() - float(meta["created_at"])
                return (
                    int(meta["schema"]) == SCHEMA_VERSION
                    and int(meta["count"]) == expected_count
                    and db.execute("SELECT count(*) FROM assets").fetchone()[0] == expected_count
                    and 0 <= age < max_age
                )
        except (sqlite3.Error, KeyError, ValueError):
            return False

    def build(self, expected_count: int) -> None:
        """Publish atomically only after a complete listing with distinct asset IDs."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(
            prefix=".listing-", suffix=".sqlite3", dir=self.path.parent
        )
        os.close(fd)
        try:
            with sqlite3.connect(temporary) as db:
                db.executescript("""
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE masters (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE assets (
                        id TEXT PRIMARY KEY, rank INTEGER UNIQUE NOT NULL,
                        created REAL, master_id TEXT NOT NULL, payload TEXT NOT NULL
                    );
                    CREATE INDEX assets_created ON assets(created, rank);
                """)
                count = 0
                for photo in self.album:
                    master = sanitize_record(photo._master_record)
                    asset = sanitize_record(photo._asset_record)
                    created = photo.asset_date.timestamp() if photo.has_asset_date else None
                    db.execute(
                        "INSERT OR REPLACE INTO masters VALUES (?, ?)",
                        (master["recordName"], json.dumps(master)),
                    )
                    db.execute(
                        "INSERT INTO assets VALUES (?, ?, ?, ?, ?)",
                        (
                            asset["recordName"],
                            count,
                            created,
                            master["recordName"],
                            json.dumps(asset),
                        ),
                    )
                    count += 1
                    if count % 1000 == 0:
                        logger.info("Cached metadata for %d of %d assets", count, expected_count)
                if count != expected_count:
                    raise ValueError(
                        f"Incomplete listing cache: received {count} of {expected_count} assets; old snapshot retained"
                    )
                db.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [
                        ("schema", str(SCHEMA_VERSION)),
                        ("count", str(count)),
                        ("created_at", str(time.time())),
                    ],
                )
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def photos(
        self,
        created_before: float | None = None,
        created_after: float | None = None,
        date_order: str = "library",
    ) -> Iterator[PhotoAsset]:
        ordering = {
            "library": "a.rank",
            "ascending": "a.created IS NULL, a.created ASC, a.id ASC",
            "descending": "a.created IS NULL, a.created DESC, a.id ASC",
        }
        if date_order not in ordering:
            raise ValueError("Unknown date order")
        clauses = []
        values = []
        if created_before is not None:
            clauses.append("(a.created IS NULL OR a.created >= ?)")
            values.append(created_before)
        if created_after is not None:
            clauses.append("(a.created IS NULL OR a.created <= ?)")
            values.append(created_after)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # Missing dates stay eligible for the normal filter/fallback behavior.
        query = (
            "SELECT m.payload, a.payload FROM assets a JOIN masters m ON m.id=a.master_id"
            + where
            + " ORDER BY "
            + ordering[date_order]
        )
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as db:
            for master, asset in db.execute(query, values):
                yield SnapshotPhoto(json.loads(master), json.loads(asset), self.album)

    def selected_count(
        self, created_before: float | None = None, created_after: float | None = None
    ) -> int:
        clauses = []
        values = []
        if created_before is not None:
            clauses.append("(created IS NULL OR created >= ?)")
            values.append(created_before)
        if created_after is not None:
            clauses.append("(created IS NULL OR created <= ?)")
            values.append(created_after)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as db:
            return int(db.execute("SELECT count(*) FROM assets" + where, values).fetchone()[0])


def prepare_snapshot(
    directory: str,
    account: str,
    domain: str,
    album: PhotoAlbum,
    expected_count: int,
    max_age: int,
    refresh: bool,
) -> ListingSnapshot:
    snapshot = ListingSnapshot(cache_path(Path(directory), account, domain, album), album)
    if refresh or not snapshot.valid(expected_count, max_age):
        logger.info("Building listing cache for %s; this first pass still enumerates iCloud", album)
        snapshot.build(expected_count)
    else:
        logger.info(
            "Using cached metadata for %s (%d assets); same-count changes may remain until refresh",
            album,
            expected_count,
        )
    return snapshot
