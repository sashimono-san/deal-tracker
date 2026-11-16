import argparse
import fcntl
import json
import os
from pathlib import Path

from .collection import ingest, preview_notifications
from .models import CollectionResult
from .notifications import deliver_pending, enqueue
from .pi_harness import collect
from .runner import ExecutionFailure
from .settings import Settings
from .store import Conflict, Store, utc_now


def run_job(store: Store, settings: Settings, job: dict) -> dict:
    watch = {**job["watch"], "shop_research": store.shop_research(job["watch"])}
    try:
        try:
            result = collect(watch, job["id"], settings)
        except (ExecutionFailure, TimeoutError, OSError) as error:
            result = CollectionResult(
                request_id=job["id"], watch_version=watch["version"],
                outcome="timed_out" if isinstance(error, TimeoutError) else "failed",
                diagnostics=str(error)[:1000],
            )
            result._usage = getattr(error, "usage", None)
        saved = ingest(store, watch["id"], result, source="pi")
        if result.shop_research is not None:
            store.record_shop_research(watch, result.shop_research, saved["id"])
        preview_notifications(store, watch["id"])
        enqueue(store, watch["id"], saved["id"], settings)
        store.finish_check(job["id"], result.outcome, result.diagnostics, saved["id"])
        return {
            "id": job["id"], "outcome": result.outcome, "run_id": saved["id"],
            "model": settings.pi_model, "effort": settings.pi_thinking, "usage": result._usage,
        }
    except Conflict as error:
        store.finish_check(job["id"], "discarded", str(error))
        return {"id": job["id"], "outcome": "discarded"}
    except Exception:
        store.finish_check(job["id"], "failed", "Worker failed before completing this check")
        raise


def run_due(store: Store, settings: Settings, limit: int | None = None) -> dict:
    if limit is not None and not 1 <= limit <= 100:
        raise ValueError("A worker pass handles one to 100 searches")
    batch_limit = min(limit or settings.daily_check_limit, settings.daily_check_limit)
    with store.path.with_suffix(".worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"outcome": "busy", "processed": 0}
        store.set_runtime("worker_last_seen", utc_now())
        issues = settings.collector_issues()
        if not settings.scheduler_enabled:
            return {"outcome": "disabled", "processed": 0, "issues": issues}
        if issues:
            return {
                "outcome": "collector_disabled", "processed": 0, "issues": issues,
                "email": deliver_pending(store, settings),
            }
        completed = []
        for _index in range(batch_limit):
            job = store.claim_check(
                lease_seconds=settings.check_timeout + 60,
                daily_limit=settings.daily_check_limit,
            )
            if job is None:
                break
            completed.append(run_job(store, settings, job))
        store.set_runtime("worker_last_seen", utc_now())
        return {
            "outcome": "ok", "processed": len(completed), "jobs": completed,
            "email": deliver_pending(store, settings),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one bounded daily pi collection batch")
    parser.add_argument("--database", type=Path, default=Path(os.environ.get("DEAL_TRACKER_DB", "data/tracker.sqlite3")))
    parser.add_argument("--once", action="store_true", help="Run one batch and exit (the default)")
    parser.add_argument("--limit", type=int, choices=range(1, 101), help="Optional lower batch limit; defaults to the daily allowance")
    args = parser.parse_args()
    store = Store(args.database)
    settings = Settings.from_environment()
    print(json.dumps(run_due(store, settings, args.limit)), flush=True)


if __name__ == "__main__":
    main()