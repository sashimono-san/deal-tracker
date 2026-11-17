from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from deal_tracker.models import CollectionResult, Observation, ShopResearch, WatchSpec
from deal_tracker.notifications import history
from deal_tracker.pi_harness import CollectionFailure
from deal_tracker.settings import Settings
from deal_tracker.store import Store
from deal_tracker.worker import main, run_due


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    store = Store(tmp_path / "tracker.sqlite3")
    settings = Settings(
        collection_enabled=True, scheduler_enabled=True, pi_directory=tmp_path,
        pi_provider="fixture", pi_model="fixture",
    )
    monkeypatch.setattr("deal_tracker.settings.shutil.which", lambda name: f"/usr/bin/{name}")
    watch = store.create(WatchSpec(
        title="Shirt", shop_url="https://example.com", country="Austria",
        currency="EUR", interval_hours=6,
    ))
    return store, settings, watch


def test_worker_processes_persisted_manual_queue_and_keeps_watch_paused(runtime, monkeypatch):
    store, settings, watch = runtime
    queued = store.queue_check(watch["id"], 1)
    assert store.queue_check(watch["id"], 1)["id"] == queued["id"]
    monkeypatch.setattr("deal_tracker.worker.collect", lambda watch, request_id, settings: CollectionResult(
        watch_version=watch["version"], request_id=request_id, outcome="blocked",
        diagnostics="Fixture: shop blocked collection",
    ))
    assert run_due(Store(store.path), settings)["processed"] == 1
    assert store.get(watch["id"])["status"] == "paused"
    assert store.jobs(watch["id"])[0]["outcome"] == "blocked"
    assert store.runs(watch["id"])[0]["source"] == "pi"
    assert store.monitoring(watch["id"])["last_outcome"] == "blocked"
    assert store.runtime("worker_last_seen") is not None
    assert run_due(store, settings)["processed"] == 0


def test_edit_before_queued_check_cancels_it_without_model_call(runtime, monkeypatch):
    store, settings, watch = runtime
    store.queue_check(watch["id"], 1)
    store.update(watch["id"], WatchSpec(title="Changed", shop_url="https://example.com"), 1)
    monkeypatch.setattr("deal_tracker.worker.collect", lambda *_args: pytest.fail("Must not collect stale work"))
    assert run_due(store, settings)["processed"] == 0
    assert store.jobs(watch["id"])[0]["outcome"] == "cancelled"


def test_edit_during_collection_discards_result(runtime, monkeypatch):
    store, settings, watch = runtime
    store.queue_check(watch["id"], 1)

    def collect(snapshot, request_id, settings):
        store.update(watch["id"], WatchSpec(title="Changed", shop_url="https://example.com"), 1)
        return CollectionResult(watch_version=1, request_id=request_id, outcome="no_change")

    monkeypatch.setattr("deal_tracker.worker.collect", collect)
    assert run_due(store, settings)["jobs"][0]["outcome"] == "discarded"
    assert store.runs(watch["id"]) == []


def test_disabled_worker_never_contacts_collector(runtime, monkeypatch):
    store, _settings, watch = runtime
    store.queue_check(watch["id"], 1)
    monkeypatch.setattr("deal_tracker.worker.collect", lambda *_args: pytest.fail("Must stay offline"))
    assert run_due(store, Settings())["outcome"] == "disabled"
    assert store.jobs(watch["id"])[0]["outcome"] == "queued"


def test_worker_reports_usage_even_when_evidence_validation_fails(runtime, monkeypatch):
    store, settings, watch = runtime
    store.queue_check(watch["id"], watch["version"])
    usage = {"complete": True, "total_tokens": 1200}

    def reject(*_args):
        raise CollectionFailure("Invalid evidence quote", usage)

    monkeypatch.setattr("deal_tracker.worker.collect", reject)
    job = run_due(store, settings)["jobs"][0]
    assert job["outcome"] == "failed"
    assert job["usage"] == usage
    assert job["model"] == "fixture"
    assert job["effort"] == "low"


@pytest.mark.parametrize("limit, expected", [(None, 25), (3, 3), (101, 25)])
def test_batch_drains_due_watches_without_a_daily_quota(runtime, monkeypatch, limit, expected):
    store, settings, watch = runtime
    watches = [watch, *[
        store.create(WatchSpec(
            title=f"Search {index}", shop_url="https://example.com", country="Austria",
            currency="EUR", interval_hours=24,
        )) for index in range(24)
    ]]
    for current in watches:
        store.set_status(current["id"], "active", current["version"])
    monkeypatch.setattr("deal_tracker.worker.collect", lambda snapshot, request_id, configuration: CollectionResult(
        watch_version=snapshot["version"], request_id=request_id, outcome="no_change",
    ))
    assert run_due(store, settings, limit=limit)["processed"] == expected
    assert run_due(Store(store.path), settings)["processed"] == 25 - expected
    assert run_due(store, settings)["processed"] == 0
    assert store.checks_today() == 25


@pytest.mark.parametrize("limit", [0, -1])
def test_explicit_worker_batch_limit_must_be_positive(runtime, limit):
    store, settings, _watch = runtime
    with pytest.raises(ValueError, match="positive"):
        run_due(store, settings, limit=limit)


def test_cli_runs_once_without_an_explicit_batch_limit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["worker", "--database", str(tmp_path / "tracker.sqlite3")])
    runner = MagicMock(side_effect=[{"outcome": "ok"}, AssertionError("Must not poll")])
    monkeypatch.setattr("deal_tracker.worker.run_due", runner)
    main()
    assert runner.call_count == 1
    assert runner.call_args.args[2] is None
    assert '"outcome": "ok"' in capsys.readouterr().out


def test_first_shop_research_is_reused_without_changing_watch_schedules(runtime, monkeypatch):
    store, settings, watch = runtime
    second = store.create(WatchSpec(
        title="Another item", shop_url=watch["shop_url"], country=watch["country"],
    ))
    store.queue_check(watch["id"], watch["version"])
    store.queue_check(second["id"], second["version"])
    contexts = []

    def collect(snapshot, request_id, configuration):
        contexts.append(snapshot["shop_research"])
        return CollectionResult(
            watch_version=snapshot["version"], request_id=request_id, outcome="no_change",
            shop_research=ShopResearch(outcome="not_found", summary="No published timing in fixture pages"),
        )

    monkeypatch.setattr("deal_tracker.worker.collect", collect)
    assert run_due(store, settings)["processed"] == 2
    assert contexts[0] is None
    assert contexts[1]["outcome"] == "not_found"
    assert Store(store.path).shop_research(second)["outcome"] == "not_found"
    assert store.get(second["id"])["schedule"] is None
    assert store.shop_research({**second, "country": "Different market"}) is None


def test_worker_collects_qualifies_and_delivers_once_across_restart(runtime, monkeypatch):
    store, settings, watch = runtime
    watch = store.update(watch["id"], WatchSpec(
        title="Synthetic shirt", shop_url="https://example.com", sku="SHIRT-1",
        audience="Me", country="Austria", currency="EUR", target_price=50, interval_hours=6,
    ), watch["version"])
    watch = store.set_status(watch["id"], "active", watch["version"])
    settings = settings.model_copy(update={
        "email_enabled": True, "smtp_host": "smtp.example.com",
        "email_from": "alerts@example.com", "email_to": "recipient@example.com",
    })

    def collect(snapshot, request_id, configuration):
        return CollectionResult(
            watch_version=snapshot["version"], request_id=request_id, outcome="success",
            diagnostics="Synthetic source inspected successfully",
            observations=[Observation(
                title="Synthetic shirt", url="https://example.com/shirt",
                evidence="Synthetic fixture: shirt in stock at EUR 40", observed_at=datetime.now(UTC),
                sku="SHIRT-1", country="Austria", currency="EUR", price=40, availability="in_stock",
            )],
        )

    monkeypatch.setattr("deal_tracker.worker.collect", collect)
    sender = MagicMock(return_value={"status": "sent", "error": ""})
    monkeypatch.setattr("deal_tracker.notifications.send_delivery", sender)
    first = run_due(store, settings)
    assert first["processed"] == first["email"]["sent"] == 1
    assert store.monitoring(watch["id"])["last_error"] == ""
    assert store.jobs(watch["id"])[0]["diagnostics"] == "Synthetic source inspected successfully"
    assert store.runs(watch["id"])[0]["preview"]["decisions"][0]["decision"] == "eligible"
    restarted = Store(store.path)
    restarted.queue_check(watch["id"], watch["version"])
    second = run_due(restarted, settings)
    assert second["processed"] == 1
    assert second["email"]["sent"] == 0
    assert sender.call_count == 1
    assert len(store.candidates(watch["id"])) == len(history(store, watch["id"])) == 1