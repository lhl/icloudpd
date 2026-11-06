# Development fork and backfill

Fork: https://github.com/lhl/icloudpd

The fork's `master` tracks upstream. Development changes are stacked so each
feature can be reviewed against its dependency:

| Branch | Review base | Change |
| --- | --- | --- |
| `feature/listing-cache` | `master` | Complete, private metadata snapshots and lazy resource lookups |
| `feature/cache-date-seek` | `feature/listing-cache` | Direct date-range selection and selected progress totals |
| `feature/cached-date-order` | `feature/cache-date-seek` | Stable ascending/descending creation-date traversal |
| `dev` | Integration branch | All three features and development notes |

No upstream pull requests were opened. Before submitting, discuss the feature
with upstream per CONTRIBUTING.md and clean up/rebase the stacked review commits
as needed. Separate branches do not make their dependencies disappear.

## Installed development checkout

Checkout: `/home/lhl/github/lhl/icloudpd`, on `dev`.
Editable environment: `/home/lhl/github/lhl/icloudpd/.venv`.
Local launcher: `/home/lhl/.local/bin/icloudpd-dev`.
The original `icloudpd` 1.32.3 executable was not replaced.

```sh
/home/lhl/.local/bin/icloudpd-dev --version
/home/lhl/.local/bin/icloudpd-dev --help
```

The launcher reports the actual checkout/package version; upstream source has
placeholder build-version constants until its release tooling stamps them.
Switching this editable checkout's branch changes the code the launcher runs.
Do not switch branches while a download process is running.

## Resume the existing archive

Confirm `/run/media/lhl/Extreme SSD` is mounted before starting. Keep the same
filenames, timezone, requested sizes, and destination as the original download.
The full-library metadata cache has **not** been populated by the bounded live
tests. First use will enumerate the whole library before downloading anything;
a later run can reuse the finished cache and select the cutoff directly.

```sh
env TZ=UTC /home/lhl/.local/bin/icloudpd-dev \
  --username 'YOUR_APPLE_ACCOUNT_EMAIL' \
  --cookie-directory "$HOME/.pyicloud" \
  --library PrimarySync \
  --folder-structure '{:%Y/%m/%d}' \
  --file-match-policy name-id7 \
  --keep-unicode-in-filenames \
  --size original --size adjusted --size alternative \
  --live-photo-size original \
  --live-photo-mov-filename-policy original \
  --log-level info \
  --listing-cache-directory "$HOME/.cache/icloudpd-listings" \
  --listing-cache-max-age 604800 \
  --skip-created-after '2020-01-13' \
  --date-order descending \
  --directory '/run/media/lhl/Extreme SSD/icloud'
```

Replace the account placeholder. The cutoff includes overlap with the last
known completed date. Existing files are checked under the original match
policy. Ascending order is also available by changing `--date-order` to
`ascending`; it starts at the oldest selected asset rather than near the last
downloaded date. Omit the date cutoff on later full-library reconciliation runs.

The example deliberately allows reuse for seven days during a static backfill.
**Count checks cannot detect same-count library changes.** New metadata/date/
membership changes can remain stale until expiry or `--refresh-listing-cache`.
Refresh after changing the remote library or for a final full reconciliation.
The cache is not authoritative evidence of complete synchronization. Cache mode
rejects all deletion/pruning flags. See [snapshot semantics](listing-cache.md).

No automatic restart or network retry loop was added: the connection failure
that stopped the old download can still occur. A completed snapshot makes
restarting cheaper; an interrupted initial cache build is discarded, not reused
as if complete. Credentials are never included in tracked files.

## Checks

Use the isolated environment for repository checks:

```sh
.venv/bin/python -m pytest -q -n 4
.venv/bin/mypy src
.venv/bin/ruff check .
```

Live validation authenticated using the existing keyring/cookie session, built a
two-asset album snapshot, verified chronological order/reuse, and downloaded one
563,384-byte original through a freshly looked-up resource URL. The test data
is private under `~/.local/state/iphone-backup/icloudpd-dev-live-6zsi80y_/`, not
in the media archive or Git. This is bounded validation, not a whole-library
cache build or completeness audit.
