import base64
import json
import smtplib
import ssl
import sys
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime
from uuid import uuid4

from .collection import evaluate, event_identity
from .models import CollectionResult, Observation
from .runner import ExecutionFailure, execute
from .settings import Settings
from .store import Conflict, MissingRecord, Store, utc_now


def qualifies(watch: dict, observation: Observation) -> tuple[bool, str, str]:
    decision, reason = evaluate(watch, observation)
    if decision == "eligible":
        return True, decision, reason
    if decision != "review" or not watch["notify_review"]:
        return False, decision, reason
    for field in ("size", "color", "country", "currency"):
        if watch[field] and watch[field].casefold() != getattr(observation, field).casefold():
            return False, "suppressed", f"Requested {field} was not confirmed"
    if watch["target_price"] is not None and (
        observation.price is None or observation.price <= 0
        or observation.price > watch["target_price"]
    ):
        return False, "suppressed", "Price target was not confirmed"
    return True, decision, reason


def enqueue(store: Store, watch_id: str, run_id: str, settings: Settings) -> int:
    if settings.email_issues():
        return 0
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        watch = store.read(connection, watch_id)
        run = connection.execute(
            "SELECT * FROM runs WHERE id = ? AND watch_id = ?", (run_id, watch_id)
        ).fetchone()
        if run is None:
            raise MissingRecord("Run not found")
        result = CollectionResult.model_validate_json(run["result"])
        if watch["status"] != "active" or run["source"] != "pi" or result.outcome != "success":
            return 0
        if result.watch_version != watch["version"]:
            return 0
        queued = 0
        for observation in result.observations:
            eligible, decision, reason = qualifies(watch, observation)
            if not eligible:
                continue
            identity = event_identity(observation)
            previous = connection.execute(
                """SELECT price FROM deliveries WHERE watch_id = ? AND identity = ?
                   AND status != 'cancelled'""", (watch_id, identity),
            ).fetchall()
            if previous and (
                observation.price is None
                or any(row["price"] is not None and row["price"] <= observation.price for row in previous)
            ):
                continue
            timestamp = utc_now()
            cursor = connection.execute(
                """INSERT OR IGNORE INTO deliveries
                   (id, watch_id, watch_version, run_id, identity, price, data, decision, reason,
                    sender, recipient, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid4().hex, watch_id, watch["version"], run_id, identity, observation.price,
                 observation.model_dump_json(), decision, reason, settings.email_from,
                 settings.email_to, timestamp, timestamp),
            )
            queued += cursor.rowcount
        return queued


def history(store: Store, watch_id: str) -> list[dict]:
    with store.connection() as connection:
        return [{**dict(row), "data": json.loads(row["data"])} for row in connection.execute(
            "SELECT * FROM deliveries WHERE watch_id = ? ORDER BY created_at DESC LIMIT 50", (watch_id,)
        )]


def resolve_delivery(store: Store, watch_id: str, delivery_id: str, action: str, acknowledged: bool) -> None:
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM deliveries WHERE id = ? AND watch_id = ?", (delivery_id, watch_id)
        ).fetchone()
        if row is None:
            raise MissingRecord("Delivery not found")
        if row["status"] not in {"failed", "uncertain"}:
            raise Conflict("Only failed or uncertain deliveries can be resolved")
        if action == "retry" and row["status"] == "uncertain" and not acknowledged:
            raise Conflict("Check your inbox and acknowledge possible duplication before retrying")
        states = {"retry": "pending", "mark_sent": "sent", "dismiss": "dismissed"}
        if action not in states:
            raise Conflict("Unknown delivery action")
        connection.execute(
            "UPDATE deliveries SET status = ?, error = '', lease_until = NULL, updated_at = ? WHERE id = ?",
            (states[action], utc_now(), delivery_id),
        )


def render_message(delivery: dict) -> bytes:
    observation = Observation.model_validate_json(delivery["data"])
    message = EmailMessage(policy=SMTP_POLICY)
    message["From"] = delivery["sender"]
    message["To"] = delivery["recipient"]
    prefix = "Review candidate" if delivery["decision"] == "review" else "Price target met"
    message["Subject"] = f"{prefix}: {' '.join(observation.title.split())[:160]}"
    message["Message-ID"] = f"<{delivery['id']}@deal-tracker.local>"
    message["Date"] = format_datetime(datetime.now(UTC))
    price = f"{observation.price:g} {observation.currency}" if observation.price is not None else "Unknown"
    shipping = f"{observation.shipping:g} {observation.currency}" if observation.shipping is not None else "Unknown"
    quote = "\n".join(f"> {line}" for line in observation.evidence.splitlines())
    message.set_content(
        f"{prefix}: {observation.title}\n\n{delivery['reason']}\n"
        f"Item price: {price}\nShipping: {shipping}\nStock: {observation.availability}\n"
        f"SKU: {observation.sku or 'Unknown'}\nSize: {observation.size or 'Unknown'}\n"
        f"Color: {observation.color or 'Unknown'}\nMarket: {observation.country or 'Unknown'}\n"
        f"Observed: {observation.observed_at.isoformat()}\nSource: {observation.url}\n\n"
        f"Quoted evidence:\n{quote}\n\nInterpretation: {observation.reason or 'None'}\n"
        "\nVerify the shop's current price, shipping and conditions before buying.\n"
    )
    return message.as_bytes()


def smtp_handoff(settings: Settings, message: bytes) -> dict:
    client = None
    handoff = False
    try:
        if settings.smtp_security == "tls":
            client = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=10, context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        handoff = True
        refused = client.sendmail(settings.email_from, [settings.email_to], message)
        return {"status": "failed" if refused else "sent", "error": "Recipient refused" if refused else ""}
    except (smtplib.SMTPException, OSError) as error:
        rejected = isinstance(error, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)) or (
            isinstance(error, smtplib.SMTPResponseException) and 400 <= error.smtp_code < 600
        )
        uncertain = handoff and not rejected
        return {
            "status": "uncertain" if uncertain else "failed",
            "error": "SMTP handoff was not confirmed; check the inbox before retrying" if uncertain
            else f"SMTP delivery failed ({type(error).__name__})",
        }
    finally:
        if client is not None:
            client.close()


def send_delivery(delivery: dict, settings: Settings) -> dict:
    configuration = {
        field: getattr(settings, field) for field in (
            "smtp_host", "smtp_port", "smtp_security", "smtp_username", "email_from", "email_to",
        )
    }
    configuration["smtp_password"] = settings.smtp_password.get_secret_value()
    payload = json.dumps({
        "settings": configuration, "message": base64.b64encode(render_message(delivery)).decode(),
    }).encode()
    try:
        raw = execute([sys.executable, "-m", "deal_tracker.notifications"], payload, 45, 16_000)
        result = json.loads(raw)
        if result["status"] not in {"sent", "failed", "uncertain"}:
            raise ValueError("Invalid delivery result")
        return result
    except OSError:
        return {"status": "failed", "error": "Could not start the mail process"}
    except (ExecutionFailure, TimeoutError, ValueError, KeyError, TypeError):
        return {"status": "uncertain", "error": "Mail process stopped without confirming delivery; check the inbox"}


def deliver_pending(store: Store, settings: Settings, limit: int = 5) -> dict:
    totals = {"sent": 0, "failed": 0, "uncertain": 0, "cancelled": 0}
    with store.connection() as connection:
        connection.execute(
            """UPDATE deliveries SET status = 'uncertain', updated_at = ?,
               error = 'Worker stopped during delivery; check the inbox before retrying'
               WHERE status = 'sending' AND lease_until <= ?""", (utc_now(), utc_now()),
        )
    if settings.email_issues():
        return totals
    for _index in range(min(limit, 5)):
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deliveries WHERE status = 'pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                break
            delivery = dict(row)
            watch = store.read(connection, row["watch_id"])
            observation = Observation.model_validate_json(row["data"])
            latest = connection.execute(
                """SELECT * FROM runs WHERE watch_id = ? AND source = 'pi'
                   ORDER BY created_at DESC, id DESC LIMIT 1""", (row["watch_id"],),
            ).fetchone()
            superseded = False
            if latest and latest["id"] != row["run_id"]:
                current_result = CollectionResult.model_validate_json(latest["result"])
                if current_result.outcome in {"success", "partial"}:
                    superseded = not any(
                        event_identity(current) == row["identity"]
                        and current.price == observation.price and qualifies(watch, current)[0]
                        for current in current_result.observations
                    )
            if (
                watch["status"] != "active" or watch["version"] != row["watch_version"]
                or row["sender"] != settings.email_from or row["recipient"] != settings.email_to
                or not qualifies(watch, observation)[0] or superseded
            ):
                connection.execute(
                    "UPDATE deliveries SET status = 'cancelled', updated_at = ?, error = ? WHERE id = ?",
                    (utc_now(), "Watch, destination or offer changed before delivery", row["id"]),
                )
                totals["cancelled"] += 1
                continue
            connection.execute(
                """UPDATE deliveries SET status = 'sending', attempts = attempts + 1,
                   lease_until = ?, updated_at = ? WHERE id = ?""",
                ((datetime.now(UTC) + timedelta(seconds=90)).isoformat(), utc_now(), row["id"]),
            )
        result = send_delivery(delivery, settings)
        with store.connection() as connection:
            connection.execute(
                """UPDATE deliveries SET status = ?, error = ?, lease_until = NULL, updated_at = ?
                   WHERE id = ? AND status = 'sending'""",
                (result["status"], result["error"][:1000], utc_now(), delivery["id"]),
            )
        totals[result["status"]] += 1
    return totals


if __name__ == "__main__":
    request = json.loads(sys.stdin.buffer.read(64_000))
    print(json.dumps(smtp_handoff(
        Settings.model_validate(request["settings"]), base64.b64decode(request["message"], validate=True),
    )))