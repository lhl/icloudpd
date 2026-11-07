"""Opt-in, complete metadata snapshots; download URLs are resolved only when needed."""

import copy
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from icloudpd.listing_changes import RebuildRequired, anchor_token
from icloudpd.listing_reconcile import apply_changes
from pyicloud_ipd.services.photos import PhotoAlbum, PhotoAsset, download_asset

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 2


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

    def build(self, expected_count: int, *, track_changes: bool = False) -> None:
        """Publish only after complete enumeration and concurrent-change checks."""
        token = anchor_token(self.album) if track_changes else None
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
                    raise RebuildRequired(
                        f"Incomplete listing cache: received {count} of {expected_count} assets; old snapshot retained"
                    )
                if token is not None:
                    # A rank scan is not an atomic snapshot. Reject concurrent
                    # relevant mutations instead of publishing a mixed view.
                    from icloudpd.listing_changes import changes
                    from icloudpd.listing_reconcile import relevant

                    for records, next_token in changes(self.album, token):
                        if any(relevant(record) for record in records):
                            raise RebuildRequired(
                                "Library changed during enumeration; retry a full cache refresh"
                            )
                        token = next_token
                    db.execute("INSERT INTO metadata VALUES (?, ?)", ("sync_token", token))
                db.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [
                        ("schema", str(SCHEMA_VERSION)),
                        ("count", str(count)),
                        ("created_at", str(time.time())),
                    ],
                )
            os.replace(temporary, self.path)
        except sqlite3.IntegrityError as error:
            if track_changes:
                raise RebuildRequired(
                    "Duplicate assets during cache enumeration; retry a full listing"
                ) from error
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def synchronize(self) -> int:
        """Commit record changes and the final token in one SQLite transaction."""
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            meta = dict(db.execute("SELECT key, value FROM metadata"))
            token = meta.get("sync_token")
            if not token:
                raise RebuildRequired("Snapshot has no change token")
            next_token, changed = apply_changes(db, self.album, token, sanitize_record)
            count = db.execute("SELECT count(*) FROM assets").fetchone()[0]
            # Read after applying the delta; fail closed if the library moved
            # again or membership could not be reconciled completely.
            if count != len(self.album):
                raise RebuildRequired("Reconciled asset count differs from iCloud")
            db.execute("UPDATE metadata SET value=? WHERE key='sync_token'", (next_token,))
            db.execute("UPDATE metadata SET value=? WHERE key='count'", (str(count),))
        return changed

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


@contextmanager
def cache_writer_lock(path: Path):
    """Serialize full replacements and incremental writes across processes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    with sqlite3.connect(lock_path, timeout=0) as lock:
        lock.execute("BEGIN IMMEDIATE")
        yield


def prepare_snapshot(
    directory: str,
    account: str,
    domain: str,
    album: PhotoAlbum,
    expected_count: int,
    max_age: int,
    refresh: bool,
    refresh_mode: str = "auto",
) -> ListingSnapshot:
    path = cache_path(Path(directory), account, domain, album)
    with cache_writer_lock(path):
        return _prepare_snapshot(path, album, expected_count, max_age, refresh, refresh_mode)


def _prepare_snapshot(
    path: Path,
    album: PhotoAlbum,
    expected_count: int,
    max_age: int,
    refresh: bool,
    refresh_mode: str,
) -> ListingSnapshot:
    if refresh_mode not in ("auto", "full", "reuse"):
        raise ValueError("Unknown cache refresh mode")
    snapshot = ListingSnapshot(path, album)
    rebuild = refresh or refresh_mode == "full"
    if refresh_mode == "reuse" and not refresh:
        if not snapshot.valid(expected_count, max_age):
            raise ValueError("No matching reusable cache; run with --cache-refresh auto or full")
        logger.warning("Reusing metadata without a change check (--cache-refresh reuse)")
        return snapshot
    if not rebuild:
        try:
            with sqlite3.connect(f"{snapshot.path.resolve().as_uri()}?mode=ro", uri=True) as db:
                meta = dict(db.execute("SELECT key, value FROM metadata"))
            count = int(meta["count"])
            rebuild = not snapshot.valid(count, max_age) or not meta.get("sync_token")
        except (sqlite3.Error, KeyError, ValueError):
            rebuild = True
    if not rebuild:
        try:
            changed = snapshot.synchronize()
            logger.info("Refreshed listing cache from change token (%d changed records)", changed)
            return snapshot
        except RebuildRequired as error:
            logger.info("Rebuilding listing cache: %s", error)
    # Full enumeration is retried once if mutations invalidate its pre-scan
    # anchor. Never publish a snapshot known to span relevant changes.
    attempt = 0
    while True:
        logger.info("Building listing cache for %s; enumerating iCloud", album)
        album.offset = 0
        try:
            snapshot.build(len(album), track_changes=True)
            return snapshot
        except RebuildRequired:
            if attempt:
                raise
            attempt += 1
