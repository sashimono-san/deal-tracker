import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .models import CollectionResult, Observation
from .store import Conflict, Store, utc_now


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def in_shop_scope(url: str, shop_url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").removeprefix("www.").lower()
    shop = (urlsplit(shop_url).hostname or "").removeprefix("www.").lower()
    return bool(shop) and (hostname == shop or hostname.endswith("." + shop))


def event_identity(observation: Observation) -> str:
    return fingerprint([
        observation.sku.casefold() or observation.url,
        observation.size.casefold(), observation.color.casefold(),
        observation.country.casefold(), observation.currency,
        " ".join(observation.evidence.casefold().split()) if observation.kind == "sale" else "",
    ])


def ingest(store: Store, watch_id: str, result: CollectionResult, source: str = "manual_import") -> dict:
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        watch = store.read(connection, watch_id)
        encoded = result.model_dump_json()
        existing = connection.execute(
            "SELECT * FROM runs WHERE watch_id = ? AND request_id = ?",
            (watch_id, result.request_id),
        ).fetchone()
        if existing:
            if existing["result"] != encoded or existing["source"] != source:
                raise Conflict("This import ID already belongs to a different result")
            return {"id": existing["id"], "replayed": True, "source": source}
        if watch["version"] != result.watch_version:
            raise Conflict("The watch changed since this result was collected")
        for observation in result.observations:
            if not in_shop_scope(observation.url, watch["shop_url"]):
                raise Conflict("An evidence URL is outside this watch's shop scope")
            if (watch["mode"] == "sale") != (observation.kind == "sale"):
                raise Conflict("Evidence kind does not match the watch mode")
        run_id = uuid4().hex
        now = utc_now()
        connection.execute(
            """INSERT INTO runs (id, watch_id, request_id, source, result, watch_snapshot, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, watch_id, result.request_id, source, encoded, json.dumps(watch), now),
        )
        for observation in result.observations:
            if observation.kind != "product":
                continue
            data = observation.model_dump(mode="json")
            candidate_id = fingerprint([watch_id, source, event_identity(observation)])[:32]
            connection.execute(
                """INSERT INTO candidates (id, watch_id, data, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET data = excluded.data
                   WHERE julianday(json_extract(excluded.data, '$.observed_at')) >=
                         julianday(json_extract(candidates.data, '$.observed_at'))""",
                (candidate_id, watch_id, json.dumps({**data, "source": source, "run_id": run_id}), now),
            )
        return {"id": run_id, "replayed": False, "source": source}


def evaluate(watch: dict[str, Any], observation: Observation) -> tuple[str, str]:
    now = datetime.now(UTC)
    if watch["status"] == "archived":
        return "suppressed", "Watch is archived"
    if observation.observed_at < now - timedelta(hours=24):
        return "suppressed", "Evidence is older than the 24-hour preview freshness limit"
    if observation.valid_until is not None and observation.valid_until <= now:
        return "suppressed", "Offer has expired"
    if watch["mode"] == "sale":
        if observation.sale_scope not in {"shop", "category"}:
            return "suppressed", "Evidence does not establish a shop or category sale"
        return "review", "Sale conditions need human review"
    if watch["mode"] == "discovery":
        return "review", "Similarity and personal fit need human review"
    for field in ("sku", "size", "color", "country"):
        if watch[field] and watch[field].casefold() != getattr(observation, field).casefold():
            return "suppressed", f"Requested {field} was not confirmed"
    if not watch["sku"]:
        return "review", "Set an exact SKU before automatic qualification"
    if not watch["country"]:
        return "review", "Set the delivery market before qualification"
    if not watch["currency"] or observation.currency != watch["currency"]:
        return "suppressed", "Currency is missing or different"
    if observation.availability != "in_stock":
        return "suppressed", "Availability is not confirmed in stock"
    if observation.price is None or observation.price <= 0:
        return "suppressed", "No valid positive price was observed"
    if watch["target_price"] is None:
        return "review", "No item-price target is configured"
    if Decimal(str(observation.price)) > Decimal(str(watch["target_price"])):
        return "suppressed", "Item price is above target"
    if watch["criteria"]:
        return "review", "Additional criteria need human review"
    return "eligible", "Item price meets target; shipping and offer conditions remain separate"


def preview_notifications(store: Store, watch_id: str) -> dict:
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        watch = store.read(connection, watch_id)
        run = connection.execute(
            "SELECT * FROM runs WHERE watch_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (watch_id,),
        ).fetchone()
        if run is None:
            raise Conflict("Import evidence before previewing notifications")
        result = CollectionResult.model_validate_json(run["result"])
        if result.watch_version != watch["version"]:
            raise Conflict("Import evidence for the current watch version first")
        decisions = []
        for observation in result.observations:
            decision, reason = evaluate(watch, observation)
            identity = event_identity(observation)
            if decision == "eligible":
                previous = connection.execute(
                    "SELECT MIN(price) AS price FROM preview_events WHERE watch_id = ? AND identity = ?",
                    (watch_id, identity),
                ).fetchone()["price"]
                if previous is not None and previous <= observation.price:
                    decision, reason = "duplicate", "Already previewed at this price or better"
                else:
                    connection.execute(
                        """INSERT INTO preview_events (id, watch_id, identity, price, data, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            uuid4().hex, watch_id, identity, observation.price,
                            observation.model_dump_json(), utc_now(),
                        ),
                    )
            decisions.append({
                "title": observation.title, "url": observation.url,
                "decision": decision, "reason": reason,
            })
        preview = {"dry_run": True, "source": run["source"], "sent": 0, "decisions": decisions}
        connection.execute("UPDATE runs SET preview = ? WHERE id = ?", (json.dumps(preview), run["id"]))
        return preview