"""Automatic database backups (alpha.96).

* **Snapshot** — a consistent copy of the live SQLite file through the online
  backup API, minus the secrets that must never leave the server (the
  DB-stored cookie/encryption secret, the Web-Push private key, unused
  invites / reset links / pairing codes). Written to ``<data>/backups/``.
* **Probe** — every snapshot is opened in a scratch connection, checked with
  ``PRAGMA integrity_check`` and its row counts compared with the live
  database. Only then is it *verified*; the readiness check (:mod:`app.readiness`)
  reports the age of the last verified backup.
* **Rotation** — seven daily, four weekly (Sunday), three monthly (1st).
* **Off-site** — the snapshot encrypted with the bridge's own key (Fernet,
  derived from ``NEXUSPRED_ENCRYPTION_KEY`` / ``SESSION_SECRET``) and pushed to
  an S3-compatible bucket (R2, B2, Hetzner, AWS …) with SigV4, or mailed to
  the admins as an attachment while it is small. Decrypt with
  ``python -m app.backups decrypt FILE.enc FILE.db`` on a host with the same key.
* **Alarm** — a snapshot that fails, an off-site push that fails, or no
  verified backup for 36 hours → one admin notice per day (event log + mail).

Runs once a day at the quiet hour (default 21:15 UTC, after the CME close),
never on the order path, and everything blocking is on a thread.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from . import config, context, crypto, db, http, security, state

log = logging.getLogger("nexuspred.backups")
_snapshot_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup")

META_KEY = "backups"
INDEX_KEY = "backups:index"
ALARM_KEY = "backups:alarm_day"
DEFAULTS: dict[str, Any] = {
    "enabled": True, "hour_utc": 21, "minute_utc": 15,
    "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 3,
    "offsite": "off",                       # off | s3 | mail
    "s3_endpoint": "", "s3_bucket": "", "s3_region": "auto", "s3_access_key": "", "s3_secret_key": "", "s3_prefix": "fluxbridge/",
    "mail_max_mb": 20,
}
SECRET_KEYS = ("s3_secret_key",)
STALE_AFTER_H = 36
BACKUP_EXCLUDED_META = ("session_secret", "vapid_private_pem")
COUNT_TABLES = ("users", "areas", "memberships", "subscriptions", "payments", "journal_trades", "agents", "push_subscriptions")
LOOP_TICK_S = 60.0
MAIL_HARD_CAP_MB = 25

_cache: Optional[dict[str, Any]] = None
_running = False


# ------------------------------------------------------------------ config
def backup_dir() -> Path:
    return Path(config.DATA_DIR) / "backups"


def get_config() -> dict[str, Any]:
    global _cache
    if _cache is None:
        cfg = dict(DEFAULTS)
        raw = db.meta_get(META_KEY)
        if raw:
            try:
                stored = json.loads(raw)
                if isinstance(stored, dict):
                    cfg.update({k: stored[k] for k in DEFAULTS if k in stored})
            except ValueError:
                pass
        for k in SECRET_KEYS:
            v = cfg.get(k) or ""
            cfg[k] = str(crypto.decrypt(v) or "") if v else ""
        _cache = cfg
    return dict(_cache)


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    global _cache
    cfg = get_config()
    if "enabled" in updates:
        cfg["enabled"] = bool(updates["enabled"])
    for k, lo, hi in (("hour_utc", 0, 23), ("minute_utc", 0, 59), ("keep_daily", 1, 60), ("keep_weekly", 0, 52), ("keep_monthly", 0, 36), ("mail_max_mb", 1, MAIL_HARD_CAP_MB)):
        if k in updates:
            try:
                n = int(float(updates[k]))
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{k} must be a whole number")
            if not lo <= n <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            cfg[k] = n
    if "offsite" in updates:
        o = str(updates["offsite"] or "off").lower()
        if o not in ("off", "s3", "mail"):
            raise ValueError("offsite must be off, s3 or mail")
        cfg["offsite"] = o
    for k in ("s3_endpoint", "s3_bucket", "s3_region", "s3_access_key", "s3_prefix"):
        if k in updates:
            v = str(updates[k] or "").strip()
            if len(v) > 300 or any(ch.isspace() for ch in v):
                raise ValueError(f"{k} looks wrong")
            cfg[k] = v
    for k in SECRET_KEYS:
        if k in updates and updates[k] != "********":
            cfg[k] = str(updates[k] or "").strip()
    if cfg["s3_endpoint"] and not cfg["s3_endpoint"].startswith("https://"):
        raise ValueError("the S3 endpoint must start with https://")
    if cfg["offsite"] == "s3" and not (cfg["s3_endpoint"] and cfg["s3_bucket"] and cfg["s3_access_key"] and cfg["s3_secret_key"]):
        raise ValueError("S3 needs endpoint, bucket, access key and secret key")
    if cfg["s3_prefix"] and not cfg["s3_prefix"].endswith("/"):
        cfg["s3_prefix"] += "/"
    stored = dict(cfg)
    for k in SECRET_KEYS:
        stored[k] = crypto.encrypt(cfg[k]) if cfg[k] else ""
    db.meta_set(META_KEY, json.dumps(stored))
    _cache = dict(cfg)
    return dict(cfg)


def public_config() -> dict[str, Any]:
    cfg = get_config()
    return {**{k: ("********" if cfg[k] else "") for k in SECRET_KEYS}, **{k: cfg[k] for k in DEFAULTS if k not in SECRET_KEYS}}


def reset() -> None:
    global _cache, _running
    _cache = None
    _running = False


# ------------------------------------------------------------------ index
def _index() -> list[dict[str, Any]]:
    raw = db.meta_get(INDEX_KEY)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
        return rows if isinstance(rows, list) else []
    except ValueError:
        return []


def _save_index(rows: list[dict[str, Any]]) -> None:
    db.meta_set(INDEX_KEY, json.dumps(rows))


def list_backups() -> list[dict[str, Any]]:
    """Newest first; entries whose file vanished are dropped."""
    rows = _index()
    kept = [r for r in rows if (backup_dir() / r["name"]).exists()]
    if len(kept) != len(rows):
        _save_index(kept)
    return sorted(kept, key=lambda r: r["created_at"], reverse=True)


def last_verified() -> Optional[dict[str, Any]]:
    for r in list_backups():
        if r.get("verified"):
            return r
    return None


def status() -> dict[str, Any]:
    """What the readiness check and the admin page show."""
    cfg = get_config()
    lv = last_verified()
    age_h = None
    if lv:
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(lv["created_at"])).total_seconds() / 3600
        except ValueError:
            age_h = None
    rows = list_backups()
    return {"enabled": cfg["enabled"], "count": len(rows), "last": rows[0] if rows else None, "last_verified": lv,
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "stale": bool(cfg["enabled"] and (age_h is None or age_h > STALE_AFTER_H)),
            "next_at": next_run_at().isoformat(), "offsite": cfg["offsite"], "running": _running,
            "dir": str(backup_dir()), "free_bytes": free_bytes()}


def free_bytes() -> Optional[int]:
    try:
        return int(shutil.disk_usage(str(config.DATA_DIR)).free)
    except OSError:
        return None


# ------------------------------------------------------------------ snapshot
def write_snapshot(path: str) -> None:
    """A consistent copy of the live database via SQLite's online backup API,
    minus :data:`BACKUP_EXCLUDED_META` and every unused one-time capability."""
    db.init()
    src = sqlite3.connect(str(db.DB_FILE))
    dst = sqlite3.connect(path)
    try:
        with dst:
            src.backup(dst)
        with dst:
            dst.executemany("DELETE FROM meta WHERE key=?", [(k,) for k in BACKUP_EXCLUDED_META])
            dst.execute("DELETE FROM password_resets WHERE used_at IS NULL")
            dst.execute("DELETE FROM invites WHERE used_by IS NULL")
            dst.execute("DELETE FROM agent_pairings")
        # VACUUM rewrites the whole file: it needs the snapshot's size again in
        # free space. On a full disk that fails halfway, so it is skipped (with a
        # warning) rather than risking the snapshot that is otherwise complete.
        need = Path(path).stat().st_size * 2 + (16 << 20)
        free = free_bytes()
        if free is not None and free < need:
            log.warning("backup: skipping VACUUM, %.0f MB free but %.0f MB needed", free / 1e6, need / 1e6)
        else:
            dst.execute("VACUUM")           # the deleted rows must not survive in free pages
    finally:
        dst.close()
        src.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for t in COUNT_TABLES:
        try:
            out[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        except sqlite3.Error:
            out[t] = -1
    return out


def probe(path: Path) -> dict[str, Any]:
    """Open the copy read-only, integrity-check it, compare row counts with the live database."""
    res: dict[str, Any] = {"integrity": "", "verified": False, "mismatch": []}
    try:
        live = sqlite3.connect(str(db.DB_FILE))
        try:
            live_counts = _counts(live)
        finally:
            live.close()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            res["integrity"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            copy_counts = _counts(conn)
        finally:
            conn.close()
        # the live database may have moved on by a few rows since the copy: allow a small drift
        res["mismatch"] = [t for t in COUNT_TABLES if abs(copy_counts.get(t, -1) - live_counts.get(t, -1)) > max(2, live_counts.get(t, 0) // 100)]
        res["verified"] = res["integrity"] == "ok" and not res["mismatch"]
        res["counts"] = copy_counts
    except sqlite3.Error as exc:
        res["integrity"] = f"error: {exc}"
    return res


def _roles_for(now: datetime) -> list[str]:
    roles = ["daily"]
    if now.weekday() == 6:
        roles.append("weekly")
    if now.day == 1:
        roles.append("monthly")
    return roles


def rotate(rows: list[dict[str, Any]], cfg: Optional[dict[str, Any]] = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep the newest N per role; a snapshot survives while any role needs it.
    Returns (kept rows, names to delete)."""
    cfg = cfg or get_config()
    keep = {"daily": cfg["keep_daily"], "weekly": cfg["keep_weekly"], "monthly": cfg["keep_monthly"]}
    ordered = sorted(rows, key=lambda r: r["created_at"], reverse=True)
    needed: set[str] = set()
    for role, n in keep.items():
        for r in [r for r in ordered if role in (r.get("roles") or ["daily"])][:n]:
            needed.add(r["name"])
    # Recency alone would rotate away the last *verified* snapshot as soon as a
    # few newer ones fail their integrity check — exactly when it is needed.
    newest_verified = next((r for r in ordered if r.get("verified")), None)
    if newest_verified:
        needed.add(newest_verified["name"])
    for r in ordered:
        if r.get("pinned"):
            needed.add(r["name"])
    kept = [r for r in ordered if r["name"] in needed]
    return kept, [r["name"] for r in ordered if r["name"] not in needed]


def _make_snapshot_sync(reason: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"fluxbridge-{now.strftime('%Y%m%d-%H%M%S')}.db"
    path = d / name
    tmp = d / (name + ".part")
    write_snapshot(str(tmp))
    os.replace(tmp, path)
    pr = probe(path)
    entry = {"name": name, "created_at": now.isoformat(), "size": path.stat().st_size, "sha256": _sha256(path),
             "verified": pr["verified"], "integrity": pr["integrity"], "mismatch": pr["mismatch"], "roles": _roles_for(now),
             "reason": reason, "offsite": "", "offsite_error": ""}
    rows = _index()
    rows.append(entry)
    kept, drop = rotate(rows)
    for n in drop:
        try:
            (d / n).unlink()
        except OSError:
            pass
    _save_index(kept)
    return entry


def _update_entry(name: str, **fields: Any) -> None:
    rows = _index()
    for r in rows:
        if r["name"] == name:
            r.update(fields)
    _save_index(rows)


# ------------------------------------------------------------------ encryption
def encrypt_file(src: Path, dst: Path) -> None:
    dst.write_bytes(crypto._get().encrypt(src.read_bytes()))


def decrypt_file(src: Path, dst: Path) -> None:
    from cryptography.fernet import InvalidToken
    data = src.read_bytes()
    try:
        plain = crypto._get().decrypt(data)
    except InvalidToken:
        for f in crypto._legacy_keys():
            try:
                plain = f.decrypt(data)
                break
            except InvalidToken:
                continue
        else:
            raise ValueError("this backup was encrypted under another key (NEXUSPRED_ENCRYPTION_KEY / SESSION_SECRET)")
    dst.write_bytes(plain)


# ------------------------------------------------------------------ S3 (SigV4)
def _sigv4_headers(cfg: dict[str, Any], method: str, url_path: str, body_sha: str, host: str, now: datetime) -> dict[str, str]:
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope_date = now.strftime("%Y%m%d")
    region = cfg["s3_region"] or "auto"
    headers = {"host": host, "x-amz-content-sha256": body_sha, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, url_path, "", *(f"{k}:{headers[k]}" for k in sorted(headers)), "", signed, body_sha])
    scope = f"{scope_date}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    k = _h(_h(_h(_h(("AWS4" + cfg["s3_secret_key"]).encode(), scope_date), region), "s3"), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={cfg['s3_access_key']}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return {"Authorization": auth, "x-amz-date": amz_date, "x-amz-content-sha256": body_sha}


async def s3_put(cfg: dict[str, Any], key: str, data: bytes) -> str:
    """PUT ``data`` at ``key`` in the configured bucket (path-style URL). Returns the object URL."""
    endpoint = cfg["s3_endpoint"].rstrip("/")
    problem = await asyncio.to_thread(security.check_outbound_url, endpoint)
    if problem:
        raise RuntimeError(f"S3 endpoint rejected: {problem}")
    host = endpoint.split("://", 1)[1].split("/", 1)[0]
    url_path = "/" + quote(f"{cfg['s3_bucket']}/{key}", safe="/-_.~")
    body_sha = hashlib.sha256(data).hexdigest()
    headers = _sigv4_headers(cfg, "PUT", url_path, body_sha, host, datetime.now(timezone.utc))
    headers["Content-Type"] = "application/octet-stream"
    r = await http.client("backup").put(endpoint + url_path, content=data, headers=headers, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"S3 answered {r.status_code}: {r.text[:200]}")
    return endpoint + url_path


async def push_offsite(entry: dict[str, Any]) -> str:
    """Encrypt and ship one snapshot per the config. Returns a short description."""
    cfg = get_config()
    path = backup_dir() / entry["name"]
    if cfg["offsite"] == "off":
        return ""
    enc = path.with_suffix(".db.enc")
    await asyncio.to_thread(encrypt_file, path, enc)
    handed_over = False                      # only a queued mail owns the file afterwards
    try:
        if cfg["offsite"] == "s3":
            data = await asyncio.to_thread(enc.read_bytes)
            url = await s3_put(cfg, f"{cfg['s3_prefix']}{enc.name}", data)
            return f"s3:{url}"
        size_mb = enc.stat().st_size / (1 << 20)
        if size_mb > cfg["mail_max_mb"]:
            raise RuntimeError(f"{size_mb:.1f} MB exceeds the mail limit of {cfg['mail_max_mb']} MB — switch to S3")
        from . import alerts
        n = await alerts.notify_admins("backup", {"name": enc.name, "size": f"{size_mb:.1f} MB", "sha256": entry["sha256"][:16],
                                                  "url": (config.PUBLIC_URL or "") + "/#/settings/backups"}, attachment=str(enc))
        if not n:
            raise RuntimeError("no admin has a mail route (platform mailer off)")
        handed_over = True                   # the mailer deletes the attachment after the send
        return f"mail:{n} admin(s)"
    finally:
        if not handed_over:                  # every other path (S3, size refusal, no route, error) owns it
            try:
                enc.unlink()
            except OSError:
                pass


async def test_offsite() -> dict[str, Any]:
    cfg = get_config()
    if cfg["offsite"] == "s3":
        url = await s3_put(cfg, f"{cfg['s3_prefix']}fluxbridge-test-{int(time.time())}.txt", b"fluxbridge off-site test\n")
        return {"ok": True, "detail": url}
    if cfg["offsite"] == "mail":
        from . import mailer
        return {"ok": mailer.configured(), "detail": "platform mailer configured" if mailer.configured() else "platform mailer is off"}
    return {"ok": False, "detail": "off-site is off"}


# ------------------------------------------------------------------ run
async def run(reason: str = "scheduled") -> dict[str, Any]:
    """Snapshot → probe → rotate → off-site → alarm. Serialised; returns the entry."""
    global _running
    if _running:
        raise RuntimeError("a backup is already running")
    _running = True
    try:
        try:
            # Its own thread: a snapshot of a large database runs for seconds and
            # must never sit in front of an order-path write in the shared pool.
            entry = await asyncio.get_running_loop().run_in_executor(_snapshot_pool, _make_snapshot_sync, reason)
        except Exception as exc:  # noqa: BLE001
            await _alarm("snapshot failed", f"{type(exc).__name__}: {exc}")
            raise
        if not entry["verified"]:
            await _alarm("backup not verified", f"{entry['name']}: integrity {entry['integrity']!r}, mismatch {entry['mismatch']}")
        try:
            where = await push_offsite(entry)
            _update_entry(entry["name"], offsite=where, offsite_error="")
            entry["offsite"] = where
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"[:300]
            _update_entry(entry["name"], offsite_error=err)
            entry["offsite_error"] = err
            await _alarm("off-site push failed", err)
        with context.use_area(context.DEFAULT_AREA_ID):
            state.log_event("info" if entry["verified"] else "warn",
                            f"Backup {entry['name']} written ({entry['size'] // 1024} KB, {'verified' if entry['verified'] else 'NOT verified'}"
                            + (f", off-site {entry['offsite']}" if entry.get("offsite") else "") + ")")
        return entry
    finally:
        _running = False


async def _alarm(title: str, detail: str) -> None:
    """One admin notice per UTC day, whatever goes wrong."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with context.use_area(context.DEFAULT_AREA_ID):
        state.log_event("error", f"Backup: {title} — {detail}")
    if db.meta_get(ALARM_KEY) == today:
        return
    db.meta_set(ALARM_KEY, today)
    try:
        from . import alerts
        await alerts.notify_admins("notice", {"title": f"Backup: {title}", "message": detail + " — Settings → Backups.",
                                              "button": "Open Backups", "url": (config.PUBLIC_URL or "") + "/#/settings/backups"})
    except Exception as exc:  # noqa: BLE001
        log.warning("backup alarm mail failed: %s", exc)


def next_run_at(now: Optional[datetime] = None) -> datetime:
    cfg = get_config()
    now = now or datetime.now(timezone.utc)
    at = now.replace(hour=cfg["hour_utc"], minute=cfg["minute_utc"], second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return at


def due(now: Optional[datetime] = None) -> bool:
    cfg = get_config()
    if not cfg["enabled"]:
        return False
    now = now or datetime.now(timezone.utc)
    at = now.replace(hour=cfg["hour_utc"], minute=cfg["minute_utc"], second=0, microsecond=0)
    if now < at:
        return False
    last = db.meta_get("backups:last_day")
    return last != now.strftime("%Y-%m-%d")


async def tick() -> Optional[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    if not due(now):
        # stale check even when a run is not due (e.g. every run fails to write)
        st = status()
        if st["stale"] and st["enabled"] and st["count"] == 0 and db.meta_get("backups:last_day"):
            await _alarm("no verified backup", f"none in the last {STALE_AFTER_H} h")
        return None
    db.meta_set("backups:last_day", now.strftime("%Y-%m-%d"))
    try:
        return await run("scheduled")
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduled backup failed: %s", exc)
        return None


async def backup_loop() -> None:
    while True:
        try:
            await tick()
        except Exception as exc:  # noqa: BLE001
            log.warning("backup tick failed: %s", exc)
        await asyncio.sleep(LOOP_TICK_S)


def delete(name: str) -> bool:
    rows = _index()
    keep = [r for r in rows if r["name"] != name]
    if len(keep) == len(rows):
        return False
    _save_index(keep)
    try:
        (backup_dir() / name).unlink()
    except OSError:
        pass
    return True


def path_of(name: str) -> Optional[Path]:
    if "/" in name or "\\" in name or not name.startswith("fluxbridge-") or not name.endswith(".db"):
        return None
    p = backup_dir() / name
    return p if p.exists() and any(r["name"] == name for r in _index()) else None


RESTORE_FILE = "restore.pending"


def schedule_restore(name: str) -> None:
    """alpha.99: the next start copies this snapshot over the live database (the
    live file is kept as fluxbridge.db.pre-rollback)."""
    if not path_of(name):
        raise ValueError("no such backup")
    (Path(config.DATA_DIR) / RESTORE_FILE).write_text(name, encoding="utf-8")


def apply_pending_restore() -> Optional[str]:
    """Called before the database is opened at startup."""
    marker = Path(config.DATA_DIR) / RESTORE_FILE
    if not marker.exists():
        return None
    name = marker.read_text(encoding="utf-8").strip()
    marker.unlink()
    src = backup_dir() / name
    if not src.exists():
        log.error("pending restore: snapshot %s is gone", name)
        return None
    # A snapshot can rot between being written and being restored. Only the
    # integrity check applies here — probe()'s row comparison is against the
    # live database, which is exactly the one being replaced.
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            tables = int(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.error("pending restore: %s cannot be opened (%s) — the live database is untouched", name, exc)
        return None
    if integrity != "ok" or tables == 0:
        log.error("pending restore: %s fails its integrity check (%s, %s tables) — the live database is untouched",
                  name, integrity, tables)
        return None
    live = Path(db.DB_FILE)
    db.disconnect()
    if live.exists():
        shutil.copy2(live, live.with_suffix(".db.pre-rollback"))
    for suffix in ("-wal", "-shm"):
        try:
            (live.parent / (live.name + suffix)).unlink()
        except OSError:
            pass
    tmp = live.with_suffix(".db.restoring")
    shutil.copy2(src, tmp)
    os.replace(tmp, live)                              # a new inode: a connection someone still holds never sees a half-written file
    log.warning("database restored from %s (previous copy kept as %s)", name, live.with_suffix(".db.pre-rollback").name)
    return name


def _cli(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "decrypt":
        decrypt_file(Path(argv[1]), Path(argv[2]))
        print(f"decrypted → {argv[2]}")
        return 0
    print("usage: python -m app.backups decrypt FILE.db.enc FILE.db", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli(sys.argv[1:]))
