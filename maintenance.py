"""Durable summaries for the four admin maintenance actions.

Only fixed result counters and reason codes are stored. Provider errors can
contain signed URLs, so exception text never enters these records or responses.
Like the existing in-process scheduler, initialization assumes one app worker.
"""
import json
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

JOBS = ("migrate_canonical", "cleanup_duplicates", "repair_strms", "scan_torbox")
COUNTERS = (
    "scanned", "ok", "renamed", "merged", "cleaned", "imported",
    "missing_strm", "orphaned_tokens", "relinked", "requeued", "skipped",
    "no_imdb", "errors", "failed",
)
NOTES = {
    "maintenance_busy": "This run was skipped because another maintenance task was running.",
    "media_unavailable": "The movie library folder was unavailable. Check the media mount.",
    "run_failed": "The run failed. Check the server logs before retrying.",
    "process_restarted": "Completion was not recorded before an application restart.",
}


class MaintenanceFailed(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def initialize():
    """Close unfinished records when the single scheduler worker starts.

    Do not retry work or infer success: a killed task may have made changes.
    """
    import db
    with db._connect() as conn:
        conn.execute(
            "UPDATE maintenance_runs SET status='interrupted', note_code='process_restarted' "
            "WHERE status='running'"
        )


def run(job_id, task, *, source="manual", **kwargs):
    """Run the existing task once and record its reported outcome."""
    import db
    if job_id not in JOBS or source not in ("manual", "scheduled"):
        raise ValueError("Unknown maintenance action or source")
    with db._connect() as conn:
        run_id = conn.execute(
            "INSERT INTO maintenance_runs(job_id,source,started_at) VALUES(?,?,?)",
            (job_id, source, _now()),
        ).lastrowid
    started = time.monotonic()
    try:
        result = task(**kwargs)
        # Deliberate allowlist: never persist paths, titles, URLs or exception text.
        counts = {k: result[k] for k in COUNTERS
                  if type(result.get(k)) is int and result[k] >= 0}
        note = result.get("deferred")
        note = note if note in ("maintenance_busy", "media_unavailable") else None
        status = "deferred" if note else (
            "partial" if counts.get("errors", 0) or counts.get("failed", 0) else "succeeded"
        )
    except Exception:
        _finish(run_id, job_id, "failed", {}, "run_failed", started)
        log.warning("Maintenance action %s failed", job_id)
        raise MaintenanceFailed(NOTES["run_failed"]) from None
    _finish(run_id, job_id, status, counts, note, started)
    return {**counts, "run_status": status, "message": NOTES.get(note)}


def _finish(run_id, job_id, status, counts, note, started):
    import db
    with db._connect() as conn:
        conn.execute(
            "UPDATE maintenance_runs SET status=?,finished_at=?,duration_seconds=?,"
            "result_json=?,note_code=? WHERE id=?",
            (status, _now(), round(time.monotonic() - started, 3),
             json.dumps(counts), note, run_id),
        )
        # Bounded history, retaining the last success even after many failed runs.
        conn.execute(
            "DELETE FROM maintenance_runs WHERE job_id=? AND status!='running' "
            "AND id NOT IN (SELECT id FROM maintenance_runs WHERE job_id=? ORDER BY id DESC LIMIT 100) "
            "AND id != COALESCE((SELECT MAX(id) FROM maintenance_runs "
            "WHERE job_id=? AND status='succeeded'), -1)",
            (job_id, job_id, job_id),
        )


def _record(row):
    if row is None:
        return None
    result = dict(row)
    result["result"] = json.loads(result.pop("result_json"))
    result["message"] = NOTES.get(result.pop("note_code"))
    return result


def status(scheduler):
    """Read-only: actual scheduler state plus durable run summaries."""
    import db
    items = []
    with db._connect() as conn:
        for job_id in JOBS:
            latest = conn.execute(
                "SELECT * FROM maintenance_runs WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
            success = conn.execute(
                "SELECT finished_at FROM maintenance_runs WHERE job_id=? AND status='succeeded' "
                "ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
            active = conn.execute(
                "SELECT * FROM maintenance_runs WHERE job_id=? AND status='running' "
                "ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
            schedule = {"mode": "manual", "interval_seconds": None, "next_run_at": None}
            if job_id == "repair_strms":
                job = scheduler.get_job("strm_repair")
                schedule["mode"] = "disabled"
                if job is not None:
                    interval = getattr(job.trigger, "interval", None)
                    schedule["interval_seconds"] = interval.total_seconds() if interval else None
                    next_run = getattr(job, "next_run_time", None)
                    schedule["next_run_at"] = next_run.isoformat() if next_run else None
                    schedule["mode"] = "automatic" if scheduler.running and next_run else "paused"
            items.append({
                "id": job_id, "schedule": schedule, "last_run": _record(latest),
                "active_run": _record(active),
                "last_success_at": success["finished_at"] if success else None,
            })
    return {"jobs": items}
