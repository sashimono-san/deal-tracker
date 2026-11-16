from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit
from uuid import uuid4

from .models import ShopResearch, WatchSpec
from .scheduling import next_check


class MissingRecord(ValueError):
    pass


class Conflict(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS watches (
                    id TEXT PRIMARY KEY,
                    spec TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'paused',
                    version INTEGER NOT NULL DEFAULT 1,
                    parent_id TEXT REFERENCES watches(id),
                    source_snapshot TEXT,
                    source_candidate_id TEXT REFERENCES candidates(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL REFERENCES watches(id),
                    data TEXT NOT NULL,
                    feedback TEXT NOT NULL DEFAULT 'unreviewed',
                    feedback_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS watch_parent ON watches(parent_id);
                CREATE INDEX IF NOT EXISTS candidate_watch ON candidates(watch_id);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL REFERENCES watches(id),
                    request_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    result TEXT NOT NULL,
                    watch_snapshot TEXT NOT NULL,
                    preview TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(watch_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS preview_events (
                    id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL REFERENCES watches(id),
                    identity TEXT NOT NULL,
                    price REAL NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_watch ON runs(watch_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS preview_identity ON preview_events(watch_id, identity);
                CREATE TABLE IF NOT EXISTS check_state (
                    watch_id TEXT PRIMARY KEY REFERENCES watches(id),
                    next_due_at TEXT,
                    lease_token TEXT,
                    lease_until TEXT,
                    last_started_at TEXT,
                    last_finished_at TEXT,
                    last_outcome TEXT,
                    last_run_id TEXT REFERENCES runs(id),
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS check_jobs (
                    id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL REFERENCES watches(id),
                    watch_version INTEGER NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'running',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    diagnostics TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS job_started ON check_jobs(started_at);
                CREATE TABLE IF NOT EXISTS runtime_state (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    watch_id TEXT NOT NULL REFERENCES watches(id),
                    watch_version INTEGER NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    identity TEXT NOT NULL,
                    price REAL,
                    data TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(watch_id, run_id, identity)
                );
                CREATE INDEX IF NOT EXISTS delivery_identity ON deliveries(watch_id, identity);
                CREATE TABLE IF NOT EXISTS shop_research (
                    host TEXT NOT NULL,
                    country TEXT NOT NULL,
                    data TEXT NOT NULL,
                    source_run_id TEXT NOT NULL REFERENCES runs(id),
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY (host, country)
                );
                PRAGMA user_version = 3;
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def decode(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **WatchSpec.model_validate_json(row["spec"]).model_dump(mode="json"),
            **{key: row[key] for key in row.keys() if key not in {"spec", "source_snapshot"}},
            "source_snapshot": json.loads(row["source_snapshot"]) if row["source_snapshot"] else None,
        }

    def read(self, connection: sqlite3.Connection, watch_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        if row is None:
            raise MissingRecord("Watch not found")
        return self.decode(row)

    def get(self, watch_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            return self.read(connection, watch_id)

    def list(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM watches ORDER BY created_at DESC, id").fetchall()
            return [self.decode(row) for row in rows]

    def create(
        self,
        spec: WatchSpec,
        parent_id: str | None = None,
        expected_version: int | None = None,
        candidate_id: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = None
            if parent_id:
                parent = self.read(connection, parent_id)
                if parent["version"] != expected_version:
                    raise Conflict("Source changed. Reopen the expansion before saving.")
                snapshot = {
                    "watch_id": parent_id,
                    "version": parent["version"],
                    "captured_at": utc_now(),
                    "spec": {key: parent[key] for key in WatchSpec.model_fields},
                }
                if candidate_id:
                    candidate = connection.execute(
                        "SELECT * FROM candidates WHERE id = ? AND watch_id = ?",
                        (candidate_id, parent_id),
                    ).fetchone()
                    if candidate is None:
                        raise MissingRecord("Candidate does not belong to the source watch")
                    if spec.mode != "exact":
                        raise Conflict("Promote a candidate into an exact-product watch")
                    snapshot["candidate"] = json.loads(candidate["data"])
            elif candidate_id:
                raise Conflict("A candidate needs a source watch")
            watch_id = uuid4().hex
            now = utc_now()
            connection.execute(
                """INSERT INTO watches
                   (id, spec, parent_id, source_snapshot, source_candidate_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    watch_id, spec.model_dump_json(), parent_id,
                    json.dumps(snapshot) if snapshot else None, candidate_id, now, now,
                ),
            )
            return self.read(connection, watch_id)

    def update(self, watch_id: str, spec: WatchSpec, version: int) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.read(connection, watch_id)
            if current["version"] != version:
                raise Conflict("This watch changed. Reload it before saving.")
            if current["mode"] != spec.mode:
                raise Conflict("Use Expand to create a watch in another mode")
            connection.execute(
                """UPDATE watches SET spec = ?, version = version + 1, updated_at = ?,
                   status = CASE WHEN status = 'active' THEN 'paused' ELSE status END WHERE id = ?""",
                (spec.model_dump_json(), utc_now(), watch_id),
            )
            connection.execute(
                "UPDATE check_state SET next_due_at = NULL WHERE watch_id = ?", (watch_id,)
            )
            return self.read(connection, watch_id)

    def set_status(self, watch_id: str, status: str, version: int) -> dict[str, Any]:
        if status not in {"active", "paused", "archived"}:
            raise Conflict("Invalid watch status")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.read(connection, watch_id)
            if current["version"] != version:
                raise Conflict("This watch changed. Reload it before saving.")
            if status == "active" and (
                any(not current[key] for key in ("shop_url", "country", "currency"))
                or not (current["schedule"] or current["interval_hours"])
            ):
                raise Conflict("Set a shop, delivery country, currency and check interval or schedule first")
            connection.execute(
                "UPDATE watches SET status = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (status, utc_now(), watch_id),
            )
            connection.execute(
                """INSERT INTO check_state (watch_id, next_due_at) VALUES (?, ?)
                   ON CONFLICT(watch_id) DO UPDATE SET next_due_at = excluded.next_due_at""",
                (watch_id, next_check(current, datetime.fromisoformat(utc_now()), include_today=True).isoformat()
                 if status == "active" else None),
            )
            return self.read(connection, watch_id)

    def monitoring(self, watch_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            self.read(connection, watch_id)
            row = connection.execute(
                "SELECT * FROM check_state WHERE watch_id = ?", (watch_id,)
            ).fetchone()
            return dict(row) if row else {
                "watch_id": watch_id, "next_due_at": None, "lease_token": None,
                "lease_until": None, "last_started_at": None, "last_finished_at": None,
                "last_outcome": None, "last_run_id": None, "last_error": "",
            }

    def claim_check(
        self, watch_id: str | None = None, *, lease_seconds: int = 180,
        daily_limit: int = 24, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if not 1 <= lease_seconds <= 900 or not 1 <= daily_limit <= 1000:
            raise ValueError("Invalid check limits")
        moment = now or datetime.now(UTC)
        timestamp = moment.isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM check_state WHERE lease_until > ?", (timestamp,)
            ).fetchone():
                return None
            count = connection.execute(
                """SELECT COUNT(*) FROM check_jobs WHERE started_at >= ?
                   AND outcome NOT IN ('queued', 'cancelled')""",
                (moment.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),
            ).fetchone()[0]
            if count >= daily_limit:
                return None
            queued = None
            if watch_id is None:
                connection.execute(
                    """UPDATE check_jobs SET outcome = 'cancelled', finished_at = ?,
                       diagnostics = 'Watch changed before collection started'
                       WHERE outcome = 'queued' AND NOT EXISTS (
                           SELECT 1 FROM watches WHERE watches.id = check_jobs.watch_id
                           AND watches.version = check_jobs.watch_version
                           AND watches.status != 'archived')""", (timestamp,),
                )
                queued = connection.execute(
                    "SELECT * FROM check_jobs WHERE outcome = 'queued' ORDER BY started_at LIMIT 1"
                ).fetchone()
                if queued:
                    watch_id = queued["watch_id"]
            if watch_id is None:
                row = connection.execute(
                    """SELECT watches.id FROM watches JOIN check_state
                       ON watches.id = check_state.watch_id WHERE watches.status = 'active'
                       AND next_due_at <= ? ORDER BY next_due_at, watches.id LIMIT 1""",
                    (timestamp,),
                ).fetchone()
                if row is None:
                    return None
                watch_id = row["id"]
            watch = self.read(connection, watch_id)
            if watch["status"] == "archived":
                raise Conflict("Restore this search before checking it")
            connection.execute(
                """UPDATE check_jobs SET outcome = 'interrupted', finished_at = ?,
                   diagnostics = 'Worker stopped before recording a result'
                   WHERE outcome = 'running'""", (timestamp,),
            )
            connection.execute(
                """UPDATE check_state SET lease_token = NULL, lease_until = NULL,
                   last_finished_at = ?, last_outcome = 'interrupted',
                   last_error = 'Worker stopped before recording a result'
                   WHERE lease_until <= ?""", (timestamp, timestamp),
            )
            token = queued["id"] if queued else uuid4().hex
            next_due = (
                next_check(watch, moment).isoformat()
                if watch["status"] == "active" else None
            )
            connection.execute(
                """INSERT INTO check_state
                   (watch_id, next_due_at, lease_token, lease_until, last_started_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(watch_id) DO UPDATE SET
                   next_due_at = excluded.next_due_at, lease_token = excluded.lease_token,
                   lease_until = excluded.lease_until, last_started_at = excluded.last_started_at,
                   last_error = ''""",
                (watch_id, next_due, token,
                 (moment + timedelta(seconds=lease_seconds)).isoformat(), timestamp),
            )
            connection.execute(
                """INSERT INTO check_jobs (id, watch_id, watch_version, started_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                   outcome = 'running', started_at = excluded.started_at""",
                (token, watch_id, watch["version"], timestamp),
            )
            return {"id": token, "watch": watch}

    def queue_check(self, watch_id: str, version: int) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            watch = self.read(connection, watch_id)
            if watch["version"] != version:
                raise Conflict("This search changed. Refresh before checking it.")
            if watch["status"] == "archived" or not watch["shop_url"]:
                raise Conflict("Use a non-archived search with a shop URL")
            existing = connection.execute(
                """SELECT * FROM check_jobs WHERE watch_id = ?
                   AND watch_version = ? AND outcome IN ('queued', 'running')
                   AND (outcome = 'queued' OR id IN (
                       SELECT lease_token FROM check_state WHERE lease_until > ?)) LIMIT 1""",
                (watch_id, version, utc_now()),
            ).fetchone()
            if existing:
                return dict(existing)
            token = uuid4().hex
            connection.execute(
                """INSERT INTO check_jobs (id, watch_id, watch_version, outcome, started_at)
                   VALUES (?, ?, ?, 'queued', ?)""", (token, watch_id, version, utc_now()),
            )
            return {"id": token, "watch_id": watch_id, "outcome": "queued"}

    def jobs(self, watch_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT check_jobs.*, check_state.lease_until FROM check_jobs
                   LEFT JOIN check_state ON check_state.lease_token = check_jobs.id
                   WHERE check_jobs.watch_id = ? ORDER BY started_at DESC LIMIT 30""",
                (watch_id,),
            ).fetchall()
            return [{
                **dict(row),
                "outcome": "interrupted" if row["outcome"] == "running" and (
                    not row["lease_until"] or row["lease_until"] <= utc_now()
                ) else row["outcome"],
            } for row in rows]

    def checks_today(self) -> int:
        with self.connection() as connection:
            return connection.execute(
                """SELECT COUNT(*) FROM check_jobs WHERE started_at >= ?
                   AND outcome NOT IN ('queued', 'cancelled')""",
                (datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),
            ).fetchone()[0]

    def set_runtime(self, name: str, value: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO runtime_state (name, value) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET value = excluded.value""", (name, value),
            )

    def runtime(self, name: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM runtime_state WHERE name = ?", (name,)).fetchone()
            return row["value"] if row else None

    def shop_research(self, watch: dict) -> dict | None:
        host = (urlsplit(watch["shop_url"]).hostname or "").removeprefix("www.")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM shop_research WHERE host = ? AND country = ?",
                (host, watch["country"].casefold()),
            ).fetchone()
            return {**json.loads(row["data"]), "checked_at": row["checked_at"]} if row else None

    def record_shop_research(self, watch: dict, research: ShopResearch, run_id: str) -> None:
        if research.outcome == "blocked":
            return
        host = (urlsplit(watch["shop_url"]).hostname or "").removeprefix("www.")
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO shop_research (host, country, data, source_run_id, checked_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(host, country) DO NOTHING""",
                (host, watch["country"].casefold(), research.model_dump_json(), run_id, utc_now()),
            )

    def finish_check(
        self, token: str, outcome: str, diagnostics: str = "", run_id: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = utc_now()
            connection.execute(
                """UPDATE check_jobs SET outcome = ?, finished_at = ?, diagnostics = ?
                   WHERE id = ? AND outcome = 'running'""",
                (outcome, timestamp, diagnostics[:3000], token),
            )
            connection.execute(
                """UPDATE check_state SET lease_token = NULL, lease_until = NULL,
                   last_finished_at = ?, last_outcome = ?, last_run_id = ?, last_error = ?
                   WHERE lease_token = ?""",
                     (timestamp, outcome, run_id, "" if outcome in {"success", "no_change"} else diagnostics[:3000], token),
            )

    def backup(self, destination: Path) -> None:
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise Conflict("Choose a new backup file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()

    def candidates(self, watch_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            self.read(connection, watch_id)
            rows = connection.execute(
                "SELECT * FROM candidates WHERE watch_id = ? ORDER BY created_at DESC LIMIT 100",
                (watch_id,),
            ).fetchall()
            return [
                {**json.loads(row["data"]), **{key: row[key] for key in row.keys() if key != "data"}}
                for row in rows
            ]

    def candidate(self, watch_id: str, candidate_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE id = ? AND watch_id = ?", (candidate_id, watch_id)
            ).fetchone()
            if row is None:
                raise MissingRecord("Candidate not found for this watch")
            return {**json.loads(row["data"]), "id": row["id"], "feedback": row["feedback"]}

    def feedback(self, watch_id: str, candidate_id: str, value: str, reason: str) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE candidates SET feedback = ?, feedback_reason = ? WHERE id = ? AND watch_id = ?",
                (value, reason, candidate_id, watch_id),
            )
            if cursor.rowcount != 1:
                raise MissingRecord("Candidate not found for this watch")

    def runs(self, watch_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            self.read(connection, watch_id)
            rows = connection.execute(
                "SELECT * FROM runs WHERE watch_id = ? ORDER BY created_at DESC, id DESC LIMIT 50",
                (watch_id,),
            ).fetchall()
            return [
                {
                    **dict(row),
                    "result": json.loads(row["result"]),
                    "watch_snapshot": json.loads(row["watch_snapshot"]),
                    "preview": json.loads(row["preview"]) if row["preview"] else None,
                }
                for row in rows
            ]