"""CloudKit change-feed transport. No media or account data is logged."""

import json
from typing import Any

from pyicloud_ipd.exceptions import PyiCloudAPIResponseException
from pyicloud_ipd.services.photos import PhotoAlbum


class RebuildRequired(ValueError):
    """The current snapshot cannot be reconciled safely."""


TOKEN_ERRORS = {"CHANGE_TOKEN_EXPIRED", "SYNC_TOKEN_EXPIRED", "INVALID_SYNC_TOKEN", "TOKEN_EXPIRED"}


def check_error(payload: dict[str, Any]) -> None:
    code = payload.get("serverErrorCode") or payload.get("errorCode")
    if code in TOKEN_ERRORS:
        raise RebuildRequired("The listing change token expired")
    if code:
        raise ValueError(f"CloudKit listing refresh failed ({code})")


def request(album: PhotoAlbum, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = album.session.post(
            album.service_endpoint + "/" + endpoint,
            params=album.params,
            data=json.dumps(body),
            headers={"Content-type": "text/plain"},
        ).json()
    except PyiCloudAPIResponseException as error:
        if error.code in TOKEN_ERRORS:
            raise RebuildRequired("The listing change token expired") from error
        raise
    if not isinstance(response, dict):
        raise ValueError("Invalid CloudKit listing refresh response")
    check_error(response)
    return response


def anchor_token(album: PhotoAlbum) -> str:
    """Capture a token before enumeration, without draining historical changes."""
    response = request(
        album, "records/query", album._list_query_gen(0, album.list_type, album.query_filter)
    )
    token = response.get("syncToken")
    if not isinstance(token, str) or not token:
        raise ValueError("iCloud did not supply a listing change token; old snapshot retained")
    return token


def changes(album: PhotoAlbum, token: str):
    """Yield all pages; callers advance their saved token only after commit."""
    seen = {token}
    while True:
        response = request(
            album,
            "changes/zone",
            {
                "zones": [{"zoneID": album._zone_id, "syncToken": token}],
            },
        )
        zones = response.get("zones")
        if not isinstance(zones, list) or len(zones) != 1:
            raise ValueError("Missing or ambiguous CloudKit change zone")
        zone = zones[0]
        check_error(zone)
        if zone.get("zoneID", {}).get("zoneName") != album._zone_id["zoneName"]:
            raise ValueError("CloudKit change zone did not match the selected library")
        next_token = zone.get("syncToken")
        records = zone.get("records")
        more = zone.get("moreComing", False)
        if (
            not isinstance(next_token, str)
            or not next_token
            or not isinstance(records, list)
            or type(more) is not bool
        ):
            raise ValueError("Malformed CloudKit change page")
        yield records, next_token
        if not more:
            return
        if next_token in seen:
            raise ValueError("CloudKit repeated a change token while more pages remain")
        seen.add(next_token)
        token = next_token


def lookup(album: PhotoAlbum, identifiers: set[str]) -> dict[str, dict[str, Any]]:
    result = {}
    names = sorted(identifiers)
    for start in range(0, len(names), 100):
        batch = names[start : start + 100]
        response = request(
            album,
            "records/lookup",
            {
                "zoneID": album._zone_id,
                "records": [{"recordName": name} for name in batch],
            },
        )
        for record in response.get("records", []):
            name = record.get("recordName")
            if (
                name in batch
                and record.get("recordType") in ("CPLAsset", "CPLMaster")
                and "fields" in record
            ):
                result[name] = record
        if any(name not in result for name in batch):
            raise RebuildRequired("Changed records are unavailable; a full listing is required")
    return result
