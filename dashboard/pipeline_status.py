"""Small, persistent record of pipeline attempts and validated publication."""

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _read(path: str) -> dict:
    try:
        with open(path) as status_file:
            return json.load(status_file)
    except FileNotFoundError:
        return {}


def _write(path: str, status: dict) -> None:
    """Replace the record atomically so a killed worker cannot truncate it."""
    directory = Path(path).parent
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as temporary:
            temporary_path = temporary.name
            json.dump(status, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def write_lock_owner(lock_file, kind: str, owner_pid: int, run_id: str | None = None) -> None:
    """Record which operation acquired the shared pipeline lock."""
    owner = {"kind": kind, "pid": owner_pid}
    if run_id is not None:
        owner["run_id"] = run_id
    lock_file.seek(0)
    lock_file.truncate()
    json.dump(owner, lock_file)
    lock_file.flush()


def start_run(path: str, run_id: str, trigger: str, owner_pid: int) -> None:
    status = _read(path)
    started_at = _now()
    previous_run = status.get("current_run")
    if previous_run:
        # start_run is called only after acquiring the pipeline lock. A marker
        # left by an earlier process therefore records a run that ended without
        # finish_run; detection time is observable, while the actual stop time
        # is unknown.
        status["last_interrupted_run"] = {
            **previous_run,
            "detected_at": started_at,
            "outcome": "interrupted",
        }
    status["current_run"] = {
        "id": run_id,
        "trigger": trigger,
        "started_at": started_at,
        "owner_pid": owner_pid,
    }
    if trigger == "manual":
        status["last_manual_attempt_at"] = started_at
    _write(path, status)


def finish_run(path: str, run_id: str, outcome: str) -> None:
    status = _read(path)
    current_run = status.get("current_run")
    if not current_run or current_run.get("id") != run_id:
        raise ValueError("pipeline status does not match the finishing run")

    finished_at = _now()
    status["last_run"] = {**current_run, "finished_at": finished_at, "outcome": outcome}
    status["current_run"] = None
    if outcome == "promoted":
        status["last_published_at"] = finished_at
    _write(path, status)


def _lock_is_held(lock_path: str) -> bool:
    try:
        with open(lock_path, "a") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    except OSError:
        # Unknown lock state is not evidence that an attempt was interrupted.
        return True
    return False


def _lock_owner(lock_path: str) -> dict | None:
    try:
        with open(lock_path) as lock_file:
            owner = json.load(lock_file)
        return owner if isinstance(owner, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def read_status(path: str, lock_path: str, stale_after_hours: float,
                now: datetime | None = None) -> dict:
    status = _read(path)
    current_run = status.get("current_run")
    published_at = status.get("last_published_at")
    now = now or datetime.now(timezone.utc)
    lock_held = _lock_is_held(lock_path)
    lock_owner = _lock_owner(lock_path) if lock_held else None

    age_hours = None
    if published_at:
        published = datetime.fromisoformat(published_at)
        age_hours = max(0.0, (now - published).total_seconds() / 3600)

    manual_attempt = status.get("last_manual_attempt_at")
    manual_retry_since_publish = bool(
        manual_attempt and (not published_at or manual_attempt > published_at)
    )

    current_run_state = None
    if current_run:
        if not lock_held:
            current_run_state = "interrupted"
        elif lock_owner is None:
            current_run_state = "unknown"
        elif (
            lock_owner.get("kind") == "pipeline"
            and lock_owner.get("pid") == current_run.get("owner_pid")
            and lock_owner.get("run_id") == current_run.get("id")
        ):
            try:
                os.kill(current_run["owner_pid"], 0)
                current_run_state = "running"
            except (OSError, TypeError, ValueError):
                current_run_state = "interrupted"
        else:
            # A different known operation owns the shared lock, so this run's
            # marker no longer describes the active lock holder.
            current_run_state = "interrupted"

    return {
        **status,
        "current_run_state": current_run_state,
        "published_age_hours": round(age_hours, 2) if age_hours is not None else None,
        "last_validated_publish_at": published_at,
        "stale_after_hours": stale_after_hours,
        "stale_threshold_basis": "operational_heuristic",
        "stale": age_hours >= stale_after_hours if age_hours is not None else None,
        "manual_retry_since_publish": manual_retry_since_publish,
    }
