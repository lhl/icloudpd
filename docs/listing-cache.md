# Listing metadata cache

`--cache-directory DIRECTORY` (alias `--listing-cache-directory`) opts into a
private SQLite metadata cache for each account/domain/library/album query.
The default refresh policy checks Apple's change token on **every run**, not
just newer creation dates or matching asset counts. Imported old photos and
same-count edits are therefore eligible for reconciliation.

```sh
icloudpd --username you@example.com --directory /backup/photos \
  --cache-directory ~/.cache/icloudpd-listings
```

## Refresh controls

| Option | Behavior |
| --- | --- |
| `--cache-refresh auto` | Default. Apply changes since the last committed token; rebuild if needed. |
| `--cache-refresh full` | Enumerate the whole album and replace its metadata cache. |
| `--refresh` | Shortcut for a full metadata rebuild; does not force media re-downloads. |
| `--refresh-listing-cache` | Backward-compatible spelling of `--refresh`. |
| `--cache-refresh reuse` | Explicitly skip change checks, requiring matching count and an unexpired cache; warns about stale metadata. |
| `--listing-cache-max-age SECONDS` | Force full reconciliation after this age (default 604800, seven days); 0 forces a full build every run. Auto still checks tokens before this age. |

A refresh mode other than `auto` requires a cache directory. `--refresh` and
`reuse` cannot be combined. Authentication and count checks still contact Apple;
`reuse` is not an offline mode. Do not choose `reuse` for correctness-preserving
normal synchronization.

## Initial build and updates

The first run enumerates the entire album before downloading media. It captures
a change token **before** scanning and drains changes afterward. If relevant
mutations occurred during enumeration, the mixed snapshot is discarded and the
scan is retried once. Continued mutation fails with the previous cache intact;
wait for the library to settle and retry. An incomplete or duplicate-asset
listing is never published as complete.

For unfiltered **All Photos**, incremental updates use asset record IDs, keeping
multiple assets that share one master separate. Changed records are looked up
in full rather than blindly merging partial fields. Additions, date/metadata
edits, hidden/deleted transitions and updated master resources are reconciled.
A deleted asset is removed from active metadata, **not from downloaded files**.
Unknown/ambiguous deletions, unavailable records, expired tokens, or mismatched
post-update asset counts trigger a full rebuild. Network/authentication failures
stop the run instead of silently using stale data.

Filtered smart albums and user albums reuse unchanged listings but rebuild on
relevant zone mutations: the zone-wide feed alone does not prove current album
membership. This is deliberately conservative. All Photos incremental ordering
is re-established as creation date descending with asset-ID ties after relevant
changes; equal-date server-rank ordering is not guaranteed. No changes are made
to the normal non-cached path.

All feed pages, changed metadata and the final token are committed in one SQLite
transaction. Failed lookups, interruptions or page errors roll back the whole
update. Full builds are published with an atomic file replacement. Do not run
multiple writers to the same cache; a busy database fails rather than silently
skipping an update. Old snapshot-format files are left untouched; the new format
uses a different scoped filename and requires a one-time full build.

## Privacy and download safety

Caches contain private filenames, dates, metadata and opaque change tokens.
Files are created with mode 0600; use a private directory outside Git/media
archives. Signed media URLs are stripped before storage. When a missing local
file needs downloading, the current resource URL is looked up. Changed resource
size/checksum stops the download rather than using an old path blindly; rerun
auto refresh or force a full refresh if needed.

This cache does not prove whole-library completeness or fix Apple network/
authentication failures. Deletion and pruning flags remain prohibited with
caching. Keep destination, timezone, filenames and requested sizes stable to
recognize existing downloads. Metadata refresh does not overwrite files already
considered present by the selected filename policy; edited content preservation
is a separate archive policy.

## Date seeking

With caching enabled, `--skip-created-after` and `--skip-created-before` select
only matching rows from the snapshot instead of fetching and checking every
newer/older asset again. The first cache build still lists the whole album.
Date endpoints are inclusive and interpreted with the existing timezone rules;
assets with missing dates stay eligible for the normal filtering fallback.
Progress totals reflect the selected rows, not the full library. The original
(non-cached) date-filter behavior is unchanged.

```sh
icloudpd --username you@example.com --directory /backup/photos \
  --listing-cache-directory ~/.cache/icloudpd-listings \
  --skip-created-after 2020-01-13
```

