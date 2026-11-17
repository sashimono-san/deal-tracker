import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .collection import in_shop_scope, ingest, preview_notifications
from .models import (
    CheckRequest,
    CollectionResult,
    DeliveryResolution,
    DeriveRequest,
    Feedback,
    Mode,
    StatusUpdate,
    WatchSpec,
    WatchUpdate,
    expansion_spec,
)
from .notifications import history, resolve_delivery
from .settings import Settings
from .store import Conflict, MissingRecord, Store


def create_app(database: Path | None = None, settings: Settings | None = None) -> FastAPI:
    configuration = settings or Settings.from_environment()
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.store = Store(database or Path(os.environ.get("DEAL_TRACKER_DB", "data/tracker.sqlite3")))
        yield

    application = FastAPI(title="Deal Tracker", version="1.0.2", lifespan=lifespan, docs_url=None, redoc_url=None)
    public_origin = urlsplit(configuration.public_origin)
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]", "testserver"]
    if public_origin.hostname:
        allowed_hosts.append(public_origin.hostname)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @application.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if public_origin.netloc and request.url.netloc.lower().removesuffix(":443") == public_origin.netloc:
                expected_origin = configuration.public_origin
            if origin and origin != expected_origin:
                return JSONResponse({"detail": "Cross-origin changes are not allowed"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site changes are not allowed"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON requests are required"}, status_code=415)
            body = await request.body()
            if len(body) > 256_000:
                return JSONResponse({"detail": "Request exceeds 256 KB"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(MissingRecord)
    async def missing_record(request: Request, error: MissingRecord):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @application.exception_handler(Conflict)
    async def conflict(request: Request, error: Conflict):
        return JSONResponse({"detail": str(error)}, status_code=409)

    def store() -> Store:
        return application.state.store

    def collection_gates(current: dict, scheduled: bool = True) -> list[str]:
        gates = configuration.collector_issues()
        if not configuration.scheduler_enabled:
            gates.append("The worker is disabled in server configuration")
        if not current["shop_url"] or urlsplit(current["shop_url"]).scheme != "https":
            gates.append("An HTTPS shop URL is required")
        if current["product_url"] and (
            urlsplit(current["product_url"]).scheme != "https"
            or not in_shop_scope(current["product_url"], current["shop_url"])
        ):
            gates.append("The product URL must be HTTPS and belong to the selected shop")
        if scheduled:
            for field, label in [("country", "Delivery country"), ("currency", "Currency")]:
                if not current[field]:
                    gates.append(f"{label} is missing")
            if not (current["schedule"] or current["interval_hours"]):
                gates.append("Check schedule is missing")
            if current["mode"] == "exact" and (not current["sku"] or not current["product_url"]):
                gates.append("Exact SKU and product URL are required for activation")
        return gates

    @application.get("/api/health")
    def health():
        return {
            "status": "ok", "collector": "unconfigured" if configuration.collector_issues() else "pi",
            "scheduler": "external" if configuration.scheduler_enabled else "disabled",
            "email": "dry_run_only" if configuration.email_issues() else "smtp",
            "collector_issues": configuration.collector_issues(),
            "email_issues": configuration.email_issues(),
            "pi_provider": configuration.pi_provider, "pi_model": configuration.pi_model,
            "pi_thinking": configuration.pi_thinking,
            "browser": "playwright_mcp" if configuration.browser_enabled else "http",
            "browser_headless": configuration.browser_headless,
            "worker_last_seen": store().runtime("worker_last_seen"),
            "checks_today": store().checks_today(),
        }

    @application.get("/api/watches")
    def list_watches():
        return store().list()

    @application.post("/api/watches", status_code=201)
    def create_watch(spec: WatchSpec):
        return store().create(spec)

    @application.get("/api/watches/{watch_id}")
    def watch_detail(watch_id: str):
        current = store().get(watch_id)
        monitoring = store().monitoring(watch_id)
        runs = store().runs(watch_id)
        return {
            "watch": current,
            "parent": store().get(current["parent_id"]) if current["parent_id"] else None,
            "children": [watch for watch in store().list() if watch["parent_id"] == watch_id],
            "candidates": store().candidates(watch_id),
            "runs": runs,
            "jobs": store().jobs(watch_id),
            "deliveries": history(store(), watch_id),
            "monitoring": monitoring,
            "shop_research": store().shop_research(current),
            "activation_gates": collection_gates(current),
            "check_gates": collection_gates(current, scheduled=False),
            "last_live_check": next((
                run["created_at"] for run in runs
                if run["source"] == "pi" and run["result"]["outcome"] in {"success", "no_change", "partial"}
            ), None),
            "next_scheduled_check": monitoring["next_due_at"] if configuration.scheduler_enabled else None,
        }

    @application.put("/api/watches/{watch_id}")
    def update_watch(watch_id: str, change: WatchUpdate):
        return store().update(watch_id, change.spec, change.version)

    @application.patch("/api/watches/{watch_id}/status")
    def update_status(watch_id: str, change: StatusUpdate):
        if change.status == "active":
            gates = collection_gates(store().get(watch_id))
            if gates:
                raise Conflict("; ".join(gates))
        return store().set_status(watch_id, change.status, change.version)

    @application.post("/api/watches/{watch_id}/checks", status_code=202)
    def queue_check(watch_id: str, change: CheckRequest):
        gates = collection_gates(store().get(watch_id), scheduled=False)
        if gates:
            raise Conflict("; ".join(gates))
        return store().queue_check(watch_id, change.version)

    @application.patch("/api/watches/{watch_id}/deliveries/{delivery_id}")
    def resolve_mail(watch_id: str, delivery_id: str, change: DeliveryResolution):
        if change.action == "retry" and configuration.email_issues():
            raise Conflict("; ".join(configuration.email_issues()))
        resolve_delivery(store(), watch_id, delivery_id, change.action, change.acknowledge_duplicate)
        return {"saved": True}

    @application.get("/api/watches/{watch_id}/expansion")
    def expansion(watch_id: str, mode: Mode = "discovery", candidate_id: str | None = Query(default=None, max_length=64)):
        parent = store().get(watch_id)
        draft = expansion_spec(parent, mode)
        candidate = None
        if candidate_id:
            candidate = store().candidate(watch_id, candidate_id)
            draft.mode = "exact"
            draft.title = candidate["title"][:160]
            draft.description = candidate["evidence"]
            draft.product_url = candidate["url"]
            draft.shop_url = f"{urlsplit(candidate['url']).scheme}://{urlsplit(candidate['url']).netloc}"
            for field in ("sku", "audience", "size", "color", "country", "currency"):
                setattr(draft, field, candidate[field])
        return {"spec": draft.model_dump(), "version": parent["version"], "source": parent, "candidate": candidate}

    @application.post("/api/watches/{watch_id}/derive", status_code=201)
    def derive(watch_id: str, change: DeriveRequest):
        return store().create(change.spec, watch_id, change.version, change.candidate_id)

    @application.post("/api/watches/{watch_id}/imports", status_code=201)
    def import_result(watch_id: str, result: CollectionResult):
        return ingest(store(), watch_id, result)

    @application.patch("/api/watches/{watch_id}/candidates/{candidate_id}")
    def candidate_feedback(watch_id: str, candidate_id: str, feedback: Feedback):
        store().feedback(watch_id, candidate_id, feedback.feedback, feedback.reason)
        return {"saved": True}

    @application.post("/api/watches/{watch_id}/preview")
    def notification_preview(watch_id: str):
        return preview_notifications(store(), watch_id)

    static = Path(__file__).parent / "static"
    if not (static / "index.html").exists():
        static = Path(__file__).parent.parent / "frontend" / "dist"
    application.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @application.get("/")
    def index():
        return FileResponse(static / "index.html")

    return application