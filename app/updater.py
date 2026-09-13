"""GitHub-backed auto-updater.

Checks the configured GitHub repo for a newer version and, when requested from the
dashboard, pulls the latest code and restarts the process.

Version resolution order for the "latest available" version:
  1. Latest GitHub release tag (e.g. ``v1.2.0``), if any releases exist.
  2. The ``VERSION`` file on the default branch.

The local version is read from the ``VERSION`` file in the working tree.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from . import config, http, state

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


def _norm(v: str) -> str:
    return v.strip().lstrip("vV")


async def _latest_release_tag() -> str | None:
    url = f"{API}/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/releases/latest"
    try:
        resp = await http.client("outbound").get(
            url, headers={"Accept": "application/vnd.github+json"}, timeout=15.0)
        if resp.status_code == 200:
            return resp.json().get("tag_name")
    except httpx.HTTPError:
        pass
    return None


async def _version_file_on_branch() -> str | None:
    url = (
        f"{RAW}/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/"
        f"{config.GITHUB_BRANCH}/VERSION"
    )
    try:
        resp = await http.client("outbound").get(url, timeout=15.0)
        if resp.status_code == 200:
            return resp.text.strip()
    except httpx.HTTPError:
        pass
    return None


async def check_for_update() -> dict[str, Any]:
    """Compare the local version with the latest available on GitHub."""
    local = config.get_version()
    remote = await _latest_release_tag() or await _version_file_on_branch()

    result: dict[str, Any] = {
        "current_version": local,
        "latest_version": _norm(remote) if remote else None,
        "update_available": False,
        "repo": f"{config.GITHUB_OWNER}/{config.GITHUB_REPO}",
        "branch": config.GITHUB_BRANCH,
        "error": None,
    }

    if not remote:
        result["error"] = "Could not reach GitHub to check for updates"
        return result

    try:
        result["update_available"] = Version(_norm(remote)) > Version(_norm(local))
    except InvalidVersion:
        # Fall back to a plain string comparison if versions aren't semver.
        result["update_available"] = _norm(remote) != _norm(local)

    return result


PIP_TIMEOUT_S = 900          # a cold install that builds cryptography / async_rithmic on a small VPS takes minutes
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _run(cmd: list[str], timeout: float = 120) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            cmd,
            cwd=str(config.ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return out.returncode == 0, (out.stdout + out.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"timed out after {int(timeout)} s"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)


def _pip_install() -> tuple[bool, str]:
    return _run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], timeout=PIP_TIMEOUT_S)


def _tracked_runtime_db() -> str:
    """The active database's repo-relative path when git tracks it — a hard
    reset would then overwrite the live data. Empty when it lives outside the
    checkout or is untracked."""
    from . import db
    try:
        rel = db.DB_FILE.resolve().relative_to(config.ROOT_DIR.resolve())
    except ValueError:
        return ""
    ok, _ = _run(["git", "ls-files", "--error-unmatch", "--", rel.as_posix()])
    return rel.as_posix() if ok else ""


_apply_lock = asyncio.Lock()


async def apply_update() -> dict[str, Any]:
    """Pull the latest code from GitHub and schedule a restart. One at a time:
    two concurrent runs would reset the checkout under each other's pip."""
    if _apply_lock.locked():
        return {"success": False, "message": "An update is already running — wait for it to finish."}
    async with _apply_lock:
        return await _apply_update()


async def _apply_update() -> dict[str, Any]:
    # On managed hosts like Render, deploys are driven by git push, not by us.
    if os.environ.get("RENDER") or os.environ.get("NEXUSPRED_MANAGED_HOST"):
        return {
            "success": False,
            "message": (
                "Managed host (e.g. Render): updates deploy automatically when you "
                "push to GitHub, or click Manual Deploy in the host dashboard."
            ),
        }
    if not (config.ROOT_DIR / ".git").exists():
        return {
            "success": False,
            "message": (
                "Not a git checkout — run connect-git.bat (Windows) or ./connect-git.sh "
                "in the install folder once to enable one-click updates."
            ),
        }

    tracked = await asyncio.to_thread(_tracked_runtime_db)
    if tracked:
        message = (f"Update refused: the active database ({tracked}) is a file git tracks — a hard reset would overwrite it. "
                   "Stop Fluxbridge, back the database up and move it to data/ (or set NEXUSPRED_DATA_DIR), then update.")
        state.log_event("error", message)
        return {"success": False, "message": message}
    old_version = config.get_version()
    # the revision to fall back to, verified before anything moves: a message
    # where a SHA should be would turn a rollback into a second failure
    ok, head_out = await asyncio.to_thread(_run, ["git", "rev-parse", "--verify", "HEAD^{commit}"])
    old_head = head_out.strip().splitlines()[0].strip() if ok and head_out.strip() else ""
    if not _SHA_RE.match(old_head):
        return {"success": False, "message": f"Could not determine the current git revision — update not started: {head_out or 'no output'}"}
    state.log_event("info", f"Applying update from GitHub (current v{old_version}, {old_head[:10]})…")

    ok, fetch_out = await asyncio.to_thread(
        _run, ["git", "fetch", "--all", "--tags", "--prune"]
    )
    if not ok:
        return {"success": False, "message": f"git fetch failed: {fetch_out}"}

    # Hard-reset to the tracked branch. Settings live in data/ (git-ignored) so
    # they are never touched by the reset.
    ok, pull_out = await asyncio.to_thread(
        _run, ["git", "reset", "--hard", f"origin/{config.GITHUB_BRANCH}"]
    )
    if not ok:
        return {"success": False, "message": f"git update failed: {pull_out}"}

    # Dependencies must install before the new code may run: a restart into a
    # revision whose requirements are missing would not come back up. On a
    # failure the checkout goes back to the previous revision (and its
    # requirements are re-installed, best effort) and no restart is scheduled.
    dep_ok, dep_out = await asyncio.to_thread(_pip_install)
    if not dep_ok:
        rb_ok, rb_out = await asyncio.to_thread(_run, ["git", "reset", "--hard", old_head])
        if rb_ok:
            await asyncio.to_thread(_pip_install)
            config.get_version(force=True)
        state.log_event("error", f"Update: dependency install failed ({dep_out[:500]}) — "
                                 + (f"checkout restored to {old_head[:10]}, no restart" if rb_ok else f"and the rollback failed too: {rb_out[:300]}"))
        return {
            "success": False,
            "message": (
                f"Dependency installation failed ({dep_out[:200]}). The checkout was restored to the previous revision and "
                "no restart was scheduled; installed packages may differ from that revision — check the log and run "
                "pip install -r requirements.txt by hand if needed."
                if rb_ok
                else f"Dependency installation failed ({dep_out[:200]}) and the rollback failed ({rb_out[:200]}). "
                     "No restart was scheduled; restore the checkout by hand."
            ),
            "previous_version": old_version,
        }

    new_version = config.get_version(force=True)
    state.log_event("info", f"Updated v{old_version} → v{new_version}; restarting…")

    # Restart shortly after responding so the dashboard gets the response first.
    asyncio.get_event_loop().call_later(1.5, _restart)
    return {
        "success": True,
        "message": f"Updated v{old_version} → v{new_version}. Restarting…",
        "previous_version": old_version,
        "version": new_version,
        "log": pull_out,
    }


def _restart() -> None:
    """Restart so the freshly pulled code runs. Under systemd (deploy/install-server.sh,
    ``Restart=always``) a clean shutdown is enough — the unit brings the service back
    with the environment file re-read; elsewhere the process re-execs itself."""
    if os.environ.get("INVOCATION_ID") or os.environ.get("NEXUSPRED_SUPERVISED"):
        import signal
        os.kill(os.getpid(), signal.SIGTERM)      # graceful: loops stop, state is flushed
        return
    os.execv(sys.executable, [sys.executable, *sys.argv])
