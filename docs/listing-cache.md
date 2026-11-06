# Listing metadata snapshots

`--listing-cache-directory DIRECTORY` opts into a private SQLite snapshot for
each account/domain/library/album query. The first run lists the entire album
before downloading media. A snapshot is published atomically only if the yielded
asset count matches Apple's count and each CPLAsset record ID is unique. Shared
masters remain separate assets. An interrupted or incomplete build does not
replace a previous complete snapshot.

Snapshots contain private filenames, dates and metadata. Database files are
created with mode 0600; choose a private directory outside any media/Git tree.
Signed media URLs are stripped from disk. When a missing local file needs a
download, icloudpd looks up its current master/asset resource URL. If its size or
checksum changed, the download stops and asks for a cache refresh instead of
writing a newer resource under an older snapshot's path.

```sh
icloudpd --username you@example.com --directory /backup/photos \
  --listing-cache-directory ~/.cache/icloudpd-listings
```

Snapshots are reused only while their asset count matches and age is less than
`--listing-cache-max-age SECONDS` (default 86400). Use
`--refresh-listing-cache` to force a complete rebuild; max age 0 also forces
rebuilding. Account authentication and count checks still require iCloud access.
An empty library is a valid snapshot.

**This is a snapshot, not change-token synchronization.** A matching count does
not detect edits, deletions plus additions, date changes or album membership
changes that leave the count unchanged. Those changes remain invisible until
refresh/expiry. Do not use this mode as proof of whole-library completeness.
Deletion and pruning options are rejected whenever caching is enabled.

The normal non-cached path is unchanged. Existing filename, size-selection,
timezone and destination settings must remain stable to recognize old downloads.
This feature does not cure authentication failures or network disconnections.
