# Alpha.74–84 local database path migration

A database-path regression was introduced when `app/db.py` moved to
`app/db/core.py` in alpha.74. The unchanged `parent.parent` calculation then
resolved to `app/` instead of the repository root. On desktop/local installs
that do not set `NEXUSPRED_DATA_DIR`, runtime SQLite state therefore lived at
`app/data/fluxbridge.db`, inside the git checkout. The one-click updater uses
`git reset --hard`, so a tracked runtime database is not a safe location.

This fix restores the intended default to `data/fluxbridge.db`, removes the
accidentally tracked database from the repository, and makes future updates
refuse to run if the active runtime database is ever tracked by git again.
Server installs created by `deploy/install-server.sh` already use
`/var/lib/fluxbridge` and are not affected by the default-path regression.

## Required transition for affected Windows/macOS/local installs

Before installing the release that contains this fix:

1. Stop Fluxbridge so SQLite is not changing while it is copied.
2. Back up the current `app/data/fluxbridge.db` somewhere outside the repo.
3. Ensure the repository-root `data/` directory exists.
4. Copy the backed-up database to `data/fluxbridge.db`.
5. Apply the update and verify users, workspaces, settings and broker logins
   before discarding the backup.

The first transition cannot be made fully automatic by this commit: an
alpha.74–84 installation runs the *old* updater before the new code starts, and
that old updater performs the hard reset while the legacy database is still a
tracked file. The manual backup/copy is therefore part of the safe migration.
