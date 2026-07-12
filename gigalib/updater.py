"""Self-update helper for GigaLib.

Detects new commits on origin/main via a periodic `git fetch`, exposes
status to the UI, and can pull + resync deps + restart the Windows
Scheduled Task on demand.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
TASK_NAME = os.environ.get("GIGALIB_TASK_NAME", "GigaLib")

_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "current_sha": None,
    "current_short": None,
    "latest_sha": None,
    "latest_short": None,
    "ahead_by": 0,
    "commits": [],           # [{sha, short, subject, author, date}]
    "update_available": False,
    "last_fetch_at": None,   # ISO string
    "last_fetch_error": None,
    "supported": True,       # false if not a git checkout or git missing
    "reason": None,          # human explanation when unsupported
}


def _is_supported() -> bool:
    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
        return False
    if shutil.which("git") is None:
        return False
    return True


def _run_git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_current_sha() -> str | None:
    result = _run_git("rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _read_remote_sha() -> str | None:
    ref = f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"
    result = _run_git("rev-parse", ref)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _read_pending_commits(current_sha: str, latest_sha: str) -> list[dict[str, str]]:
    """Return commit metadata for commits in latest but not current."""
    if current_sha == latest_sha:
        return []
    fmt = "%H%x1f%h%x1f%s%x1f%an%x1f%ci"
    rng = f"{current_sha}..{latest_sha}"
    result = _run_git("log", f"--pretty=format:{fmt}", rng, "--no-merges")
    if result.returncode != 0:
        return []
    commits: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, short, subject, author, date = parts
        commits.append(
            {
                "sha": sha,
                "short": short,
                "subject": subject,
                "author": author,
                "date": date,
            }
        )
    return commits


def _is_working_tree_clean() -> tuple[bool, str]:
    result = _run_git("status", "--porcelain")
    if result.returncode != 0:
        return False, (result.stderr.strip() or "git status failed")
    dirty = result.stdout.strip()
    if dirty:
        return False, "Working tree has uncommitted changes"
    return True, ""


def _update_state(**changes: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(changes)


def get_status() -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def refresh_status_from_local() -> None:
    """Read current HEAD without fetching (fast)."""
    if not _is_supported():
        _update_state(supported=False, reason="Not a git checkout")
        return
    current = _read_current_sha()
    latest = _read_remote_sha()
    commits = (
        _read_pending_commits(current, latest)
        if current and latest and current != latest
        else []
    )
    _update_state(
        supported=True,
        reason=None,
        current_sha=current,
        current_short=(current[:7] if current else None),
        latest_sha=latest,
        latest_short=(latest[:7] if latest else None),
        ahead_by=len(commits),
        commits=commits,
        update_available=bool(commits),
    )


def fetch_and_refresh() -> dict[str, Any]:
    """Run `git fetch` then refresh state. Safe to call from scheduler."""
    if not _is_supported():
        _update_state(supported=False, reason="Not a git checkout")
        return get_status()
    try:
        result = _run_git("fetch", "--quiet", DEFAULT_REMOTE, DEFAULT_BRANCH, timeout=60)
        if result.returncode != 0:
            _update_state(
                last_fetch_at=datetime.utcnow().isoformat() + "Z",
                last_fetch_error=(result.stderr.strip() or "git fetch failed"),
            )
            refresh_status_from_local()
            return get_status()
    except subprocess.TimeoutExpired:
        _update_state(
            last_fetch_at=datetime.utcnow().isoformat() + "Z",
            last_fetch_error="git fetch timed out",
        )
        refresh_status_from_local()
        return get_status()
    _update_state(
        last_fetch_at=datetime.utcnow().isoformat() + "Z",
        last_fetch_error=None,
    )
    refresh_status_from_local()
    return get_status()


_BG_FETCH_LOCK = threading.Lock()
_BG_FETCH_IN_FLIGHT = False


def maybe_background_fetch(min_interval_seconds: int = 60) -> bool:
    """Kick off a background `git fetch` if enough time has passed since the last one."""
    global _BG_FETCH_IN_FLIGHT
    if not _is_supported():
        return False
    with _STATE_LOCK:
        last = _STATE.get("last_fetch_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last.rstrip("Z"))
            if (datetime.utcnow() - last_dt).total_seconds() < min_interval_seconds:
                return False
        except Exception:
            pass
    with _BG_FETCH_LOCK:
        if _BG_FETCH_IN_FLIGHT:
            return False
        _BG_FETCH_IN_FLIGHT = True

    def _worker() -> None:
        global _BG_FETCH_IN_FLIGHT
        try:
            fetch_and_refresh()
        finally:
            with _BG_FETCH_LOCK:
                _BG_FETCH_IN_FLIGHT = False

    threading.Thread(target=_worker, name="gigalib-update-fetch", daemon=True).start()
    return True


def _dep_files_changed(current_sha: str, latest_sha: str) -> bool:
    result = _run_git("diff", "--name-only", f"{current_sha}..{latest_sha}")
    if result.returncode != 0:
        return False
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return bool(changed & {"pyproject.toml", "uv.lock", "requirements.txt"})


def _spawn_detached_restart(delay_seconds: int = 3) -> bool:
    """Fire-and-forget detached PowerShell that restarts the scheduled task.

    Ends the task, waits for the previous listener on the app port to release,
    then re-runs the task. Without the port-release wait, the new instance
    often loses the bind race and exits, leaving nothing on the port.
    """
    if sys.platform != "win32":
        return False
    app_port = int(os.environ.get("GIGALIB_PORT", "5000"))
    log_path = os.path.join(REPO_ROOT, "instance", "updater-restart.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # PowerShell script: end task, poll until port is free (max 20s), then run.
    ps_log = log_path.replace("'", "''")
    script = (
        f"$ErrorActionPreference='Continue'; "
        f"$log='{ps_log}'; "
        f"function L($m) {{ Add-Content -Path $log -Value ((Get-Date -Format o) + ' ' + $m) }}; "
        f"L 'helper started, waiting {int(delay_seconds)}s'; "
        f"Start-Sleep -Seconds {int(delay_seconds)}; "
        f"L 'ending task'; "
        f"$e = schtasks /end /tn '{TASK_NAME}' 2>&1; L (\"end: \" + ($e -join ' | ')); "
        f"$deadline = (Get-Date).AddSeconds(20); "
        f"while ((Get-Date) -lt $deadline) {{ "
        f"  $listener = Get-NetTCPConnection -LocalPort {app_port} -State Listen -ErrorAction SilentlyContinue; "
        f"  if (-not $listener) {{ L 'port free'; break }}; "
        f"  Start-Sleep -Milliseconds 500 "
        f"}}; "
        f"L 'running task'; "
        f"$r = schtasks /run /tn '{TASK_NAME}' 2>&1; L (\"run: \" + ($r -join ' | ')); "
        f"L 'helper done'"
    )
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    flags = (
        DETACHED_PROCESS
        | CREATE_NEW_PROCESS_GROUP
        | CREATE_NO_WINDOW
        | CREATE_BREAKAWAY_FROM_JOB
    )

    def _spawn(creationflags: int) -> bool:
        try:
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                cwd=REPO_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
            return True
        except Exception:
            return False

    # If the parent job object forbids breakaway, Popen raises with
    # ERROR_ACCESS_DENIED. Retry without the breakaway flag so we at least try.
    if _spawn(flags):
        return True
    return _spawn(flags & ~CREATE_BREAKAWAY_FROM_JOB)


def _run_uv_sync() -> tuple[bool, str]:
    uv = shutil.which("uv")
    if uv is None:
        return False, "uv not found on PATH"
    try:
        result = subprocess.run(
            [uv, "sync"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "uv sync timed out"
    if result.returncode != 0:
        return False, (result.stderr.strip() or result.stdout.strip() or "uv sync failed")
    return True, ""


def apply_update(auto_restart: bool = True) -> dict[str, Any]:
    """Pull latest main, resync deps if needed, and (optionally) restart the task."""
    if not _is_supported():
        return {"ok": False, "error": "Updates not supported in this install"}

    # Always refresh state first so we know what we're pulling.
    fetch_and_refresh()
    status = get_status()
    if not status.get("update_available"):
        return {"ok": True, "message": "Already up to date", "restarted": False, **status}

    clean, reason = _is_working_tree_clean()
    if not clean:
        return {"ok": False, "error": reason}

    current_sha = status["current_sha"]
    latest_sha = status["latest_sha"]
    deps_changed = _dep_files_changed(current_sha, latest_sha)

    pull = _run_git("pull", "--ff-only", DEFAULT_REMOTE, DEFAULT_BRANCH, timeout=120)
    if pull.returncode != 0:
        return {
            "ok": False,
            "error": (pull.stderr.strip() or pull.stdout.strip() or "git pull failed"),
        }

    uv_message = ""
    if deps_changed:
        ok, msg = _run_uv_sync()
        if not ok:
            # Code was pulled, but deps didn't sync. Report but don't restart.
            refresh_status_from_local()
            return {
                "ok": False,
                "error": f"Pulled code but dependency sync failed: {msg}",
                "restarted": False,
            }
        uv_message = "Dependencies synced. "

    refresh_status_from_local()
    restarted = False
    if auto_restart:
        restarted = _spawn_detached_restart(delay_seconds=3)

    return {
        "ok": True,
        "message": f"{uv_message}{'Restarting service...' if restarted else 'Update applied.'}",
        "restarted": restarted,
        "deps_changed": deps_changed,
        **get_status(),
    }
