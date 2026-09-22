#!/usr/bin/env python3
"""Focused lifecycle and freshness checks for dashboard/pipeline_status.py."""
import fcntl
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard.pipeline_status import (  # noqa: E402
    finish_run,
    read_status,
    start_run,
    write_lock_owner,
)


def main():
    with tempfile.TemporaryDirectory() as directory:
        status_path = os.path.join(directory, "pipeline_status.json")
        lock_path = os.path.join(directory, "pipeline.lock")

        # Existing deployments have no status record. Do not infer publish age
        # from the DuckDB file timestamp.
        status = read_status(status_path, lock_path, stale_after_hours=48)
        assert status["last_validated_publish_at"] is None
        assert status["published_age_hours"] is None
        assert status["stale"] is None

        owner_pid = os.getpid()
        start_run(status_path, "first", "scheduled", owner_pid)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status = read_status(status_path, lock_path, stale_after_hours=48)
            assert status["current_run_state"] == "unknown"
            write_lock_owner(lock_file, "pipeline", owner_pid, "first")
            status = read_status(status_path, lock_path, stale_after_hours=48)
            assert status["current_run_state"] == "running"
        status = read_status(status_path, lock_path, stale_after_hours=48)
        assert status["current_run_state"] == "interrupted"

        # A different operation may hold the shared lock. It cannot make an
        # old pipeline marker look active.
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write_lock_owner(lock_file, "majors-scrape", owner_pid)
            status = read_status(status_path, lock_path, stale_after_hours=48)
            assert status["current_run_state"] == "interrupted"

        # The next pipeline run preserves the abandoned marker before replacing it.
        start_run(status_path, "second", "manual", owner_pid)
        status = read_status(status_path, lock_path, stale_after_hours=48)
        assert status["last_interrupted_run"]["id"] == "first"
        assert status["last_interrupted_run"]["outcome"] == "interrupted"
        assert status["current_run"]["id"] == "second"
        assert status["manual_retry_since_publish"] is True

        finish_run(status_path, "second", "promoted")
        status = read_status(status_path, lock_path, stale_after_hours=48)
        assert status["last_validated_publish_at"] is not None
        assert status["manual_retry_since_publish"] is False
        assert status["stale_threshold_basis"] == "operational_heuristic"

        # Failed or unverified runs do not refresh validated publish age.
        previous_publish = status["last_validated_publish_at"]
        start_run(status_path, "third", "manual", owner_pid)
        finish_run(status_path, "third", "rejected")
        status = read_status(
            status_path,
            lock_path,
            stale_after_hours=48,
            now=datetime.fromisoformat(previous_publish) + timedelta(hours=49),
        )
        assert status["last_validated_publish_at"] == previous_publish
        assert status["published_age_hours"] == 49.0
        assert status["stale"] is True
        assert status["manual_retry_since_publish"] is True

    print("Pipeline status lifecycle and freshness checks passed")


if __name__ == "__main__":
    main()
