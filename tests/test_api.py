from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from deal_tracker.api import create_app
from deal_tracker.settings import Settings
from deal_tracker.store import Store


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "tracker.sqlite3", Settings())) as client:
        yield client


def create_watch(client, **changes):
    spec = {
        "title": "Linen shirt", "mode": "exact", "sku": "SHIRT-123",
        "shop_url": "https://www.massimodutti.com", "audience": "Me", "size": "M",
        "color": "Green", "country": "Austria", "currency": "EUR", "target_price": 50,
        **changes,
    }
    response = client.post("/api/watches", json=spec)
    assert response.status_code == 201, response.text
    return response.json()


def evidence(**changes):
    return {
        "title": "Linen shirt", "url": "https://www.massimodutti.com/at/shirt.html",
        "observed_at": datetime.now(UTC).isoformat(), "evidence": "Fixture price and variant evidence",
        "sku": "SHIRT-123", "audience": "Me", "size": "M", "color": "Green",
        "country": "Austria", "currency": "EUR", "price": 40, "availability": "in_stock",
        **changes,
    }


def import_result(client, watch, observations=None, **changes):
    result = {
        "watch_version": watch["version"], "request_id": "test-result-0001", "outcome": "success",
        "observations": observations if observations is not None else [evidence()], **changes,
    }
    return client.post(f"/api/watches/{watch['id']}/imports", json=result)


def test_full_expansion_and_candidate_promotion_flow(client):
    parent = create_watch(client)
    draft = client.get(f"/api/watches/{parent['id']}/expansion").json()
    assert draft["spec"]["sku"] == draft["spec"]["size"] == ""
    draft["spec"].update(audience="Someone", description="Relaxed linen shirts", shop_url="https://www.massimodutti.com")
    child = client.post(f"/api/watches/{parent['id']}/derive", json={"version": draft["version"], "spec": draft["spec"]}).json()
    response = import_result(client, child, [evidence(sku="P-456", audience="Someone", size="S")])
    assert response.status_code == 201, response.text
    detail = client.get(f"/api/watches/{child['id']}").json()
    candidate = detail["candidates"][0]
    promoted_draft = client.get(f"/api/watches/{child['id']}/expansion", params={"candidate_id": candidate["id"]}).json()
    promoted = client.post(f"/api/watches/{child['id']}/derive", json={"version": child["version"], "spec": promoted_draft["spec"], "candidate_id": candidate["id"]})
    assert promoted.status_code == 201, promoted.text
    result = promoted.json()
    assert result["mode"] == "exact"
    assert result["sku"] == "P-456"
    assert result["source_candidate_id"] == candidate["id"]
    assert result["status"] == "paused"
    assert result["source_snapshot"]["candidate"]["sku"] == "P-456"
    assert client.get(f"/api/watches/{parent['id']}").json()["watch"] == parent
    assert client.get(f"/api/watches/{child['id']}").json()["children"][0]["id"] == result["id"]


def test_cross_watch_promotion_is_rejected(client):
    parent = create_watch(client, mode="discovery")
    other = create_watch(client, title="Another watch")
    assert import_result(client, parent).status_code == 201
    candidate = client.get(f"/api/watches/{parent['id']}").json()["candidates"][0]
    response = client.post(f"/api/watches/{other['id']}/derive", json={"version": 1, "spec": {"title": "Forged child"}, "candidate_id": candidate["id"]})
    assert response.status_code == 404


def test_import_is_idempotent_and_never_claims_live_collection(client):
    watch = create_watch(client)
    observation = evidence()
    first = import_result(client, watch, [observation]).json()
    second = import_result(client, watch, [observation]).json()
    assert first["id"] == second["id"]
    assert second["replayed"] is True
    detail = client.get(f"/api/watches/{watch['id']}").json()
    assert len(detail["runs"]) == len(detail["candidates"]) == 1
    assert detail["runs"][0]["source"] == "manual_import"
    assert detail["last_live_check"] is None
    assert import_result(client, watch, [evidence(price=39)]).status_code == 409


def test_notification_preview_deduplicates_without_sending(client):
    watch = create_watch(client)
    assert import_result(client, watch).status_code == 201
    first = client.post(f"/api/watches/{watch['id']}/preview", json={}).json()
    assert first["dry_run"] and first["sent"] == 0
    assert first["decisions"][0]["decision"] == "eligible"
    second = client.post(f"/api/watches/{watch['id']}/preview", json={}).json()
    assert second["decisions"][0]["decision"] == "duplicate"
    assert import_result(client, watch, [evidence(price=35)], request_id="test-result-0002").status_code == 201
    improved = client.post(f"/api/watches/{watch['id']}/preview", json={}).json()
    assert improved["decisions"][0]["decision"] == "eligible"


@pytest.mark.parametrize("changes", [
    {"price": None}, {"price": 0}, {"price": 51}, {"size": "XL"}, {"sku": "OTHER"},
    {"currency": "USD"}, {"availability": "unknown"},
    {"observed_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
    {"valid_until": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
])
def test_unqualified_evidence_never_generates_an_eligible_preview(client, changes):
    watch = create_watch(client)
    assert import_result(client, watch, [evidence(**changes)]).status_code == 201
    preview = client.post(f"/api/watches/{watch['id']}/preview", json={}).json()
    assert preview["decisions"][0]["decision"] == "suppressed"


def test_shop_sale_and_ambiguous_candidates_require_review(client):
    sale = create_watch(client, mode="sale")
    assert import_result(client, sale, [evidence(kind="sale", sale_scope="shop")]).status_code == 201
    assert client.post(f"/api/watches/{sale['id']}/preview", json={}).json()["decisions"][0]["decision"] == "review"
    discovery = create_watch(client, mode="discovery")
    assert import_result(client, discovery).status_code == 201
    detail = client.get(f"/api/watches/{discovery['id']}").json()
    candidate = detail["candidates"][0]
    response = client.patch(f"/api/watches/{discovery['id']}/candidates/{candidate['id']}", json={"feedback": "not_useful", "reason": "Wrong fit"})
    assert response.status_code == 200
    assert client.get(f"/api/watches/{discovery['id']}").json()["candidates"][0]["feedback_reason"] == "Wrong fit"


def test_invalid_evidence_is_rejected_atomically(client):
    watch = create_watch(client)
    assert import_result(client, watch, [evidence(), evidence(url="https://example.com/item")]).status_code == 409
    assert import_result(client, watch, [evidence(kind="sale")]).status_code == 409
    assert import_result(client, watch, schema_version=2).status_code == 422
    assert import_result(client, watch, watch_version=9).status_code == 409
    detail = client.get(f"/api/watches/{watch['id']}").json()
    assert detail["runs"] == detail["candidates"] == []


def test_blocked_runs_stay_visible_and_create_no_candidates(client):
    watch = create_watch(client)
    assert import_result(client, watch, [], outcome="blocked", diagnostics="Login required").status_code == 201
    detail = client.get(f"/api/watches/{watch['id']}").json()
    assert detail["runs"][0]["result"]["outcome"] == "blocked"
    assert detail["candidates"] == []
    assert client.post(f"/api/watches/{watch['id']}/preview", json={}).json()["sent"] == 0


def test_local_api_rejects_forged_lineage_cross_origin_and_large_payloads(client):
    assert client.post("/api/watches", json={"title": "Injected", "parent_id": "123"}).status_code == 422
    assert client.post("/api/watches", json={"title": "Cross origin"}, headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/api/watches", data={"title": "Form post"}).status_code == 415
    assert client.post("/api/watches", content="x" * 256_001, headers={"content-type": "application/json"}).status_code == 413
    assert client.get("/api/health", headers={"host": "attacker.example"}).status_code == 400
    assert client.get("/api/watches").json() == []


@pytest.mark.parametrize("public_origin, backend_url, browser_origin", [
    ("https://deals.example", "http://deals.example", "https://deals.example"),
    ("https://DEALS.example:443/", "http://deals.example:443", "https://deals.example"),
    ("https://deals.example:8443", "http://deals.example:8443", "https://deals.example:8443"),
])
def test_configured_public_origin_supports_https_proxy_reads_and_writes(
    tmp_path, monkeypatch, public_origin, backend_url, browser_origin,
):
    monkeypatch.setenv("DEAL_TRACKER_PUBLIC_ORIGIN", public_origin)
    with TestClient(create_app(tmp_path / "tracker.sqlite3"), base_url=backend_url) as proxy_client:
        response = proxy_client.get("/api/health")
        assert response.status_code == 200, response.text
        response = proxy_client.post(
            "/api/watches", json={"title": "Proxy search"},
            headers={"Origin": browser_origin, "Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 201, response.text
        assert proxy_client.get("/api/watches").json()[0]["title"] == "Proxy search"
        assert proxy_client.get("/api/health", headers={"Host": "localhost"}).status_code == 200


@pytest.mark.parametrize("origin", [
    "https://attacker.example", "http://deals.example", "https://deals.example:8443", "null",
])
def test_public_origin_rejects_foreign_origins(tmp_path, origin):
    settings = Settings(public_origin="https://deals.example")
    with TestClient(create_app(tmp_path / "tracker.sqlite3", settings), base_url="http://deals.example") as proxy_client:
        response = proxy_client.post("/api/watches", json={"title": "Rejected"}, headers={"Origin": origin})
        assert response.status_code == 403
        assert proxy_client.get("/api/watches").json() == []


def test_public_origin_preserves_host_and_cross_site_boundaries(tmp_path):
    settings = Settings(public_origin="https://deals.example")
    with TestClient(create_app(tmp_path / "tracker.sqlite3", settings), base_url="http://deals.example") as proxy_client:
        assert proxy_client.get("/api/health", headers={
            "Host": "attacker.example", "X-Forwarded-Host": "deals.example", "X-Forwarded-Proto": "https",
        }).status_code == 400
        assert proxy_client.post("/api/watches", json={"title": "Cross site"}, headers={
            "Origin": "https://deals.example", "Sec-Fetch-Site": "cross-site",
        }).status_code == 403
        assert proxy_client.post("/api/watches", json={"title": "Wrong host"}, headers={
            "Host": "localhost", "Origin": "https://deals.example",
        }).status_code == 403
        assert proxy_client.post("/api/watches", data={"title": "Form"}, headers={
            "Origin": "https://deals.example",
        }).status_code == 415
        assert proxy_client.get("/api/watches").json() == []


@pytest.mark.parametrize("origin", [
    "http://deals.example", "https://deals.example/path", "https://user:pass@deals.example",
    "https://deals.example?query=1", "https://deals.example/#fragment", "https://*.example",
    "https://deals.example https://other.example",
])
def test_public_origin_requires_one_https_origin(origin):
    with pytest.raises(ValueError):
        Settings(public_origin=origin)


def test_disabled_runtime_cannot_activate_or_queue_checks(client):
    watch = create_watch(client, product_url="https://www.massimodutti.com/shirt", interval_hours=6)
    assert client.patch(f"/api/watches/{watch['id']}/status", json={"version": 1, "status": "active"}).status_code == 409
    assert client.post(f"/api/watches/{watch['id']}/checks", json={"version": 1}).status_code == 409
    detail = client.get(f"/api/watches/{watch['id']}").json()
    assert detail["watch"]["status"] == "paused"
    assert detail["jobs"] == detail["deliveries"] == []
    assert detail["activation_gates"]


def test_active_watch_and_queued_check_are_versioned_and_persistent(tmp_path, monkeypatch):
    monkeypatch.setattr("deal_tracker.settings.shutil.which", lambda name: f"/usr/bin/{name}")
    settings = Settings(
        collection_enabled=True, scheduler_enabled=True, pi_directory=tmp_path,
        pi_provider="fixture", pi_model="fixture",
    )
    database = tmp_path / "tracker.sqlite3"
    with TestClient(create_app(database, settings)) as client:
        watch = create_watch(client, product_url="https://www.massimodutti.com/shirt", interval_hours=6)
        active = client.patch(f"/api/watches/{watch['id']}/status", json={"version": 1, "status": "active"})
        assert active.status_code == 200
        assert client.post(f"/api/watches/{watch['id']}/checks", json={"version": 1}).status_code == 409
        queued = client.post(f"/api/watches/{watch['id']}/checks", json={"version": 2})
        assert queued.status_code == 202
        assert queued.json()["outcome"] == "queued"
        assert client.post(f"/api/watches/{watch['id']}/checks", json={"version": 2}).json()["id"] == queued.json()["id"]
    with TestClient(create_app(database, settings)) as restarted:
        detail = restarted.get(f"/api/watches/{watch['id']}").json()
        assert detail["watch"]["status"] == "active"
        assert detail["next_scheduled_check"]
        assert detail["jobs"][0]["outcome"] == "queued"
        assert detail["activation_gates"] == []
        assert restarted.get("/api/health").json()["pi_model"] == "fixture"


def test_checks_can_be_queued_after_old_daily_allowances(tmp_path, monkeypatch):
    monkeypatch.setenv("DEAL_TRACKER_DAILY_CHECK_LIMIT", "1")
    monkeypatch.setattr(Settings, "collector_issues", lambda _settings: [])
    settings = Settings.from_environment().model_copy(update={"scheduler_enabled": True})
    database = tmp_path / "tracker.sqlite3"
    with TestClient(create_app(database, settings)) as client:
        watch = create_watch(client)
        store = Store(database)
        for _attempt in range(25):
            job = store.claim_check(watch["id"])
            store.finish_check(job["id"], "failed")
        health = client.get("/api/health").json()
        assert health["checks_today"] == 25
        assert "daily_check_limit" not in health
        response = client.post(
            f"/api/watches/{watch['id']}/checks", json={"version": watch["version"]},
        )
        assert response.status_code == 202, response.text
        assert response.json()["outcome"] == "queued"


def test_exact_watch_activates_without_size_color_or_price_target(tmp_path, monkeypatch):
    monkeypatch.setattr("deal_tracker.settings.shutil.which", lambda name: f"/usr/bin/{name}")
    settings = Settings(
        collection_enabled=True, scheduler_enabled=True, pi_directory=tmp_path,
        pi_provider="fixture", pi_model="fixture",
    )
    with TestClient(create_app(tmp_path / "tracker.sqlite3", settings)) as client:
        watch = create_watch(
            client, size="", color="", target_price=None,
            product_url="https://www.massimodutti.com/shirt", interval_hours=6,
        )
        assert client.get(f"/api/watches/{watch['id']}").json()["activation_gates"] == []
        active = client.patch(f"/api/watches/{watch['id']}/status", json={"version": 1, "status": "active"})
        assert active.status_code == 200, active.text
        current = client.get(f"/api/watches/{watch['id']}").json()["watch"]
        assert import_result(client, current, [evidence(size="S", color="Blue")]).status_code == 201
        preview = client.post(f"/api/watches/{watch['id']}/preview", json={}).json()
        assert preview["decisions"][0]["decision"] == "review"


def test_shopping_for_label_is_not_required_in_retailer_evidence(client):
    watch = create_watch(client, audience="Someone")
    assert import_result(client, watch, [evidence(audience="")]).status_code == 201
    preview = client.post(f"/api/watches/{watch['id']}/preview", json={}).json()
    assert preview["decisions"][0]["decision"] == "eligible"


def test_repeat_observations_refresh_candidate_without_losing_review_or_lineage(client):
    watch = create_watch(client, mode="discovery")
    assert import_result(client, watch).status_code == 201
    candidate = client.get(f"/api/watches/{watch['id']}").json()["candidates"][0]
    assert client.patch(f"/api/watches/{watch['id']}/candidates/{candidate['id']}", json={"feedback": "useful", "reason": "Right fit"}).status_code == 200
    draft = client.get(f"/api/watches/{watch['id']}/expansion", params={"candidate_id": candidate["id"]}).json()
    promoted = client.post(f"/api/watches/{watch['id']}/derive", json={"version": 1, "spec": draft["spec"], "candidate_id": candidate["id"]}).json()
    assert import_result(client, watch, [evidence(price=35)], request_id="repeat-result-0002").status_code == 201
    detail = client.get(f"/api/watches/{watch['id']}").json()
    assert len(detail["candidates"]) == 1
    assert detail["candidates"][0]["id"] == candidate["id"]
    assert detail["candidates"][0]["price"] == 35
    assert detail["candidates"][0]["feedback_reason"] == "Right fit"
    child = client.get(f"/api/watches/{promoted['id']}").json()["watch"]
    assert child["source_snapshot"]["candidate"]["price"] == 40