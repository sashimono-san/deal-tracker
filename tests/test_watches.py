from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from deal_tracker.models import CheckSchedule, WatchSpec, expansion_spec
from deal_tracker.scheduling import next_check
from deal_tracker.store import Conflict, MissingRecord, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "tracker.sqlite3")


def shirt(**changes):
    return WatchSpec(
        **{
            "title": "Linen shirt",
            "mode": "exact",
            "shop_url": "https://www.massimodutti.com",
            "description": "A relaxed linen shirt with a camp collar",
            "sku": "SHIRT-123",
            "audience": "Me",
            "size": "M",
            "color": "Green",
            "country": "Austria",
            "currency": "EUR",
            "target_price": 49,
            **changes,
        }
    )


def test_expansion_is_independent_and_preserves_source_snapshot(store):
    parent = store.create(shirt())
    draft = expansion_spec(parent)
    assert draft.sku == draft.size == draft.color == draft.product_url == ""
    assert draft.target_price is None
    assert draft.interval_hours is None
    draft.audience = "Someone"
    draft.shop_url = "https://www.uniqlo.com"
    child = store.create(draft, parent["id"], parent["version"])
    assert child["id"] != parent["id"]
    assert child["parent_id"] == parent["id"]
    assert child["mode"] == "discovery"
    assert child["status"] == "paused"
    assert store.get(parent["id"]) == parent
    store.update(parent["id"], shirt(description="Changed later"), parent["version"])
    assert store.get(child["id"])["source_snapshot"]["spec"]["description"] == parent["description"]
    assert store.get(child["id"])["description"] == parent["description"]


def test_lineage_survives_restart_and_archive(store):
    parent = store.create(shirt())
    child = store.create(expansion_spec(parent, "sale"), parent["id"], 1)
    store.set_status(parent["id"], "archived", 1)
    restarted = Store(store.path)
    assert restarted.get(child["id"])["parent_id"] == parent["id"]
    assert restarted.get(parent["id"])["status"] == "archived"


def test_conflicting_edits_and_missing_parents_are_rejected(store):
    parent = store.create(shirt())
    store.update(parent["id"], shirt(title="New title"), 1)
    with pytest.raises(Conflict):
        store.create(expansion_spec(parent), parent["id"], 1)
    with pytest.raises(Conflict):
        store.update(parent["id"], shirt(title="Stale edit"), 1)
    with pytest.raises(MissingRecord):
        store.create(expansion_spec(parent), "missing", 1)
    with pytest.raises(Conflict):
        store.update(parent["id"], shirt(mode="sale"), 2)
    assert len(store.list()) == 1


def test_backup_restores_watch_history(store, tmp_path):
    parent = store.create(shirt())
    child = store.create(expansion_spec(parent), parent["id"], 1)
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    assert Store(backup).get(child["id"]) == child
    with pytest.raises(Conflict):
        store.backup(backup)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://127.0.0.1", "http://192.168.1.2", "http://localhost", "https://name:secret@example.com"])
def test_non_public_or_unsafe_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        shirt(shop_url=url)


def test_unknown_fields_and_nonfinite_prices_are_rejected():
    with pytest.raises(ValidationError):
        WatchSpec(title="Hidden lineage", parent_id="forged")
    with pytest.raises(ValidationError):
        shirt(target_price=float("nan"))


def test_scheduling_is_persistent_and_paused_searches_stay_idle(store):
    watch = store.create(shirt(interval_hours=6))
    assert store.claim_check() is None
    active = store.set_status(watch["id"], "active", 1)
    restarted = Store(store.path)
    started = datetime.now(UTC)
    job = restarted.claim_check(now=started)
    assert job["watch"]["id"] == watch["id"]
    assert restarted.monitoring(watch["id"])["next_due_at"] == (
        started + timedelta(hours=6)
    ).isoformat()
    restarted.finish_check(job["id"], "no_change")
    assert restarted.claim_check(now=started + timedelta(hours=5)) is None
    assert restarted.monitoring(watch["id"])["last_outcome"] == "no_change"
    store.set_status(watch["id"], "paused", active["version"])
    assert restarted.claim_check(now=started + timedelta(hours=7)) is None


def test_check_leases_prevent_overlap_and_expire_after_a_crash(store):
    first = store.create(shirt(interval_hours=1))
    second = store.create(shirt(interval_hours=1))
    started = datetime.now(UTC)
    first_job = store.claim_check(first["id"], lease_seconds=30, now=started)
    other_worker = Store(store.path)
    assert other_worker.claim_check(second["id"], now=started) is None
    recovered = other_worker.claim_check(
        second["id"], now=started + timedelta(seconds=31)
    )
    assert recovered["watch"]["id"] == second["id"]
    store.finish_check(first_job["id"], "failed")
    assert other_worker.monitoring(second["id"])["lease_token"] == recovered["id"]
    with store.connection() as connection:
        row = connection.execute(
            "SELECT outcome FROM check_jobs WHERE id = ?", (first_job["id"],)
        ).fetchone()
    assert row["outcome"] == "interrupted"


def test_daily_check_count_does_not_limit_checks_after_restart(store):
    watch = store.create(shirt())
    for _attempt in range(25):
        job = store.claim_check(watch["id"])
        assert job is not None
        store.finish_check(job["id"], "failed")
    restarted = Store(store.path)
    assert restarted.checks_today() == 25
    assert restarted.claim_check(watch["id"]) is not None


def test_activation_requires_schedule_and_editing_pauses_active_watch(store):
    watch = store.create(shirt())
    with pytest.raises(Conflict, match="interval"):
        store.set_status(watch["id"], "active", 1)
    edited = store.update(watch["id"], shirt(interval_hours=6), 1)
    active = store.set_status(watch["id"], "active", edited["version"])
    changed = store.update(watch["id"], shirt(interval_hours=12), active["version"])
    assert changed["status"] == "paused"
    assert store.monitoring(watch["id"])["next_due_at"] is None


@pytest.mark.parametrize("repeat, interval, weekdays, moment, expected", [
    ("weekly", 1, [0, 2], "2026-11-16T08:00:00+00:00", "2026-11-17T23:00:00+00:00"),
    ("weekly", 1, [0, 2], "2026-11-18T08:00:00+00:00", "2026-11-22T23:00:00+00:00"),
    ("weekly", 2, [0, 2], "2026-11-18T08:00:00+00:00", "2026-11-29T23:00:00+00:00"),
    ("daily", 3, [], "2026-11-16T08:00:00+00:00", "2026-11-18T23:00:00+00:00"),
])
def test_calendar_recurrence(repeat, interval, weekdays, moment, expected):
    watch = shirt(schedule=CheckSchedule(
        repeat=repeat, interval=interval, weekdays=weekdays,
        start_date="2026-11-16", timezone="Europe/Vienna",
    )).model_dump(mode="json")
    assert next_check(watch, datetime.fromisoformat(moment)).isoformat() == expected


def test_calendar_keeps_local_dates_across_daylight_saving():
    watch = shirt(schedule=CheckSchedule(
        repeat="daily", start_date="2026-03-27", timezone="Europe/Vienna",
    )).model_dump(mode="json")
    assert next_check(watch, datetime(2026, 3, 28, 8, tzinfo=UTC)).isoformat() == "2026-03-28T23:00:00+00:00"
    assert next_check(watch, datetime(2026, 3, 29, 8, tzinfo=UTC)).isoformat() == "2026-03-29T22:00:00+00:00"


@pytest.mark.parametrize("period, moment, expected", [
    ("black_friday", "2026-11-16T08:00:00+00:00", "2026-11-20T00:00:00+00:00"),
    ("black_friday", "2026-11-20T08:00:00+00:00", "2026-11-21T00:00:00+00:00"),
    ("black_friday", "2026-11-30T08:00:00+00:00", "2026-12-07T00:00:00+00:00"),
    ("christmas", "2026-12-15T08:00:00+00:00", "2026-12-16T00:00:00+00:00"),
    ("new_year", "2026-12-31T08:00:00+00:00", "2027-01-01T00:00:00+00:00"),
    ("winter_sales", "2027-01-08T08:00:00+00:00", "2027-01-09T00:00:00+00:00"),
])
def test_opted_in_sale_periods_boost_only_to_daily(period, moment, expected):
    watch = shirt(schedule=CheckSchedule(
        weekdays=[0], start_date="2026-11-01", sale_periods=[period],
    )).model_dump(mode="json")
    assert next_check(watch, datetime.fromisoformat(moment)).isoformat() == expected


def test_calendar_persists_and_skips_missed_occurrences(store, monkeypatch):
    monkeypatch.setattr("deal_tracker.store.utc_now", lambda: "2026-11-15T08:00:00+00:00")
    watch = store.create(shirt(schedule=CheckSchedule(weekdays=[0, 2], start_date="2026-11-16")))
    active = store.set_status(watch["id"], "active", watch["version"])
    restarted = Store(store.path)
    assert restarted.get(watch["id"])["schedule"]["start_date"] == "2026-11-16"
    assert restarted.claim_check(now=datetime(2026, 11, 15, 8, tzinfo=UTC)) is None
    job = restarted.claim_check(now=datetime(2026, 11, 19, 8, tzinfo=UTC))
    restarted.finish_check(job["id"], "no_change")
    assert restarted.monitoring(watch["id"])["next_due_at"] == "2026-11-23T00:00:00+00:00"
    assert restarted.claim_check(now=datetime(2026, 11, 19, 9, tzinfo=UTC)) is None
    draft = expansion_spec(active)
    assert draft.schedule is None


@pytest.mark.parametrize("changes", [
    {"weekdays": []}, {"weekdays": [7]}, {"timezone": "Not/AZone"}, {"interval": 0},
])
def test_invalid_calendar_settings_are_rejected(changes):
    with pytest.raises(ValidationError):
        CheckSchedule(**{"weekdays": [0], "start_date": "2026-11-16", **changes})


def test_calendar_and_legacy_interval_cannot_both_apply():
    with pytest.raises(ValidationError, match="not both"):
        shirt(interval_hours=24, schedule=CheckSchedule(weekdays=[0], start_date="2026-11-16"))