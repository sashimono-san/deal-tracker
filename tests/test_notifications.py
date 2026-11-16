import smtplib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from deal_tracker.collection import ingest
from deal_tracker.models import CollectionResult, Observation, WatchSpec
from deal_tracker.notifications import (
    deliver_pending,
    enqueue,
    history,
    resolve_delivery,
    smtp_handoff,
)
from deal_tracker.settings import Settings
from deal_tracker.store import Conflict, Store


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "tracker.sqlite3")
    watch = store.create(WatchSpec(
        title="Shirt", shop_url="https://example.com", sku="SHIRT-1", size="M", color="Green",
        country="Austria", currency="EUR", target_price=50, interval_hours=6,
    ))
    watch = store.set_status(watch["id"], "active", 1)
    settings = Settings(
        email_enabled=True, smtp_host="smtp.example.com",
        email_from="alerts@example.com", email_to="recipient@example.com",
    )
    return store, watch, settings


def record(runtime, request_id="test-request", price=40, **changes):
    store, watch, settings = runtime
    observation = Observation(**{
        "title": "Shirt", "url": "https://example.com/shirt", "observed_at": datetime.now(UTC),
        "evidence": "Green shirt, size M, in stock", "sku": "SHIRT-1", "size": "M", "color": "Green",
        "country": "Austria", "currency": "EUR", "price": price, "availability": "in_stock", **changes,
    })
    result = CollectionResult(
        request_id=request_id, watch_version=watch["version"], outcome="success", observations=[observation],
    )
    saved = ingest(store, watch["id"], result, source="pi")
    return enqueue(store, watch["id"], saved["id"], settings)


def test_sent_alerts_deduplicate_across_runs_and_restart(runtime, monkeypatch):
    store, watch, settings = runtime
    monkeypatch.setattr("deal_tracker.notifications.send_delivery", lambda *_args: {"status": "sent", "error": ""})
    assert record(runtime) == 1
    assert deliver_pending(store, settings)["sent"] == 1
    assert record((Store(store.path), watch, settings), "same-price") == 0
    assert record(runtime, "better-price", price=35) == 1
    assert deliver_pending(store, settings)["sent"] == 1
    assert len(history(store, watch["id"])) == 2


def test_uncertain_handoff_is_not_automatically_retried(runtime, monkeypatch):
    store, watch, settings = runtime
    calls = MagicMock(return_value={"status": "uncertain", "error": "Connection lost"})
    monkeypatch.setattr("deal_tracker.notifications.send_delivery", calls)
    record(runtime)
    assert deliver_pending(store, settings)["uncertain"] == 1
    assert record(runtime, "unchanged-again") == 0
    deliver_pending(Store(store.path), settings)
    assert calls.call_count == 1
    delivery = history(store, watch["id"])[0]
    with pytest.raises(Conflict, match="acknowledge"):
        resolve_delivery(store, watch["id"], delivery["id"], "retry", False)
    resolve_delivery(store, watch["id"], delivery["id"], "retry", True)
    assert history(store, watch["id"])[0]["status"] == "pending"


@pytest.mark.parametrize("changes", [{"size": "S"}, {"price": None}, {"currency": "USD"}, {"availability": "unknown"}])
def test_unconfirmed_offers_do_not_queue_mail(runtime, changes):
    assert record(runtime, **changes) == 0


def test_pause_cancels_pending_delivery(runtime, monkeypatch):
    store, watch, settings = runtime
    record(runtime)
    store.set_status(watch["id"], "paused", watch["version"])
    monkeypatch.setattr("deal_tracker.notifications.send_delivery", lambda *_args: pytest.fail("Paused watch must not mail"))
    assert deliver_pending(store, settings)["cancelled"] == 1


def test_smtp_encrypts_before_login_and_distinguishes_uncertainty(runtime, monkeypatch):
    _store, _watch, settings = runtime
    client = MagicMock()
    client.sendmail.return_value = {}
    monkeypatch.setattr(smtplib, "SMTP", lambda *_args, **_kwargs: client)
    assert smtp_handoff(settings, b"Subject: Fixture\r\n\r\nTest")["status"] == "sent"
    assert client.starttls.call_count == 1
    client.sendmail.side_effect = smtplib.SMTPServerDisconnected("closed after DATA")
    assert smtp_handoff(settings, b"Fixture")["status"] == "uncertain"
    client.sendmail.side_effect = smtplib.SMTPDataError(550, b"Rejected")
    assert smtp_handoff(settings, b"Fixture")["status"] == "failed"


def test_disabled_email_never_queues_or_sends(runtime, monkeypatch):
    store, watch, _settings = runtime
    monkeypatch.setattr("deal_tracker.notifications.send_delivery", lambda *_args: pytest.fail("Email is disabled"))
    assert record((store, watch, Settings())) == 0
    assert deliver_pending(store, Settings())["sent"] == 0


def test_newer_offer_cancels_obsolete_pending_alert(runtime, monkeypatch):
    store, _watch, settings = runtime
    record(runtime)
    record(runtime, "now-unavailable", availability="out_of_stock")
    monkeypatch.setattr("deal_tracker.notifications.send_delivery", lambda *_args: pytest.fail("Offer is obsolete"))
    assert deliver_pending(store, settings)["cancelled"] == 1


def test_pending_unchanged_offer_is_delivered_once(runtime, monkeypatch):
    store, _watch, settings = runtime
    record(runtime)
    assert record(runtime, "still-the-same") == 0
    sender = MagicMock(return_value={"status": "sent", "error": ""})
    monkeypatch.setattr("deal_tracker.notifications.send_delivery", sender)
    assert deliver_pending(store, settings)["sent"] == 1
    assert sender.call_count == 1


def test_review_email_requires_explicit_opt_in(runtime):
    store, _watch, settings = runtime
    for enabled in (False, True):
        watch = store.create(WatchSpec(
            title="Similar shirts", mode="discovery", shop_url="https://example.com",
            country="Austria", currency="EUR", interval_hours=6, notify_review=enabled,
        ))
        watch = store.set_status(watch["id"], "active", 1)
        assert record((store, watch, settings)) == int(enabled)


def test_dismissing_failed_offer_does_not_repeat_it_on_next_check(runtime, monkeypatch):
    store, watch, settings = runtime
    monkeypatch.setattr("deal_tracker.notifications.send_delivery", lambda *_args: {"status": "failed", "error": "Fixture refusal"})
    record(runtime)
    assert deliver_pending(store, settings)["failed"] == 1
    delivery = history(store, watch["id"])[0]
    resolve_delivery(store, watch["id"], delivery["id"], "dismiss", False)
    assert history(store, watch["id"])[0]["status"] == "dismissed"
    assert record(runtime, "unchanged-after-dismissal") == 0