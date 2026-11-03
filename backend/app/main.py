"""ChargeSight FastAPI application.

Start (from the project root):   python run.py
or:                               uvicorn app.main:app --app-dir backend
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .config import PROJECT_ROOT, get_settings
from .db import SessionLocal, begin_immediate, create_schema, session_scope
from .integrations.base import RedactingFilter
from .models import SimState
from .services.seed import seed_reference_data
from .simulator import engine

FRONTEND_DIR = PROJECT_ROOT / "frontend"
log = logging.getLogger("chargesight")


def configure_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    f = RedactingFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)


def init_database() -> None:
    create_schema()
    from .services.demo_seed import autoseed_if_needed

    with session_scope() as db:
        created = seed_reference_data(db)
    if get_settings().public_snapshot_autoload:
        from .integrations.public_listings import load_snapshot, snapshot_retrieved_at
        from .services import settings_store

        with session_scope() as db:
            info = settings_store.get(db, "public_snapshot", None)
            if not info or info.get("retrieved_at") != snapshot_retrieved_at():
                res = load_snapshot(db)
                settings_store.put(db, "public_snapshot", {"retrieved_at": res.get("retrieved_at"),
                                                           "inserted": res.get("inserted"), "rejected": res.get("rejected")})
                log.info("Loaded %s public listings (Open Charge Map snapshot, CC BY 4.0).", res.get("inserted"))
    with session_scope() as db:
        demo = autoseed_if_needed(db)
        if get_settings().sim_autostart:
            db.get(SimState, 1).running = True
    from .services.workspace_repair import repair_running_without_fleet

    repair_running_without_fleet()          # v9.2: never leave an empty simulation running (backup first)
    with session_scope() as db:            # bring SLA escalation and subscriptions up to date at start (idempotent)
        from .services.events import now_for_source
        from .services.tickets import sla_sweep
        from .tenancy import set_org

        for org_id in _org_ids(db):
            set_org(db, org_id)
            from .services import billing
            billing.current(db, org_id)
            for src in ("simulation", "ocpp", "operator_api"):
                sla_sweep(db, src, now_for_source(db, src))
    if created:
        log.info("Database initialised with the synthetic demonstration fleet.")
    if demo:
        log.info("Synthetic demo dataset created (%s tickets, 6 h of history). Labelled SIMULATED.", demo["tickets"])


def _org_ids(db) -> list[int]:
    from sqlalchemy import select

    from .models_saas import Organisation

    return list(db.scalars(select(Organisation.id).order_by(Organisation.id)))


def _tick() -> None:
    """Advance every organisation's own simulated fleet (each in its own tenant-bound transaction).
    Organisations whose subscription is read-only (expired / cancelled / suspended) are not advanced."""
    from sqlalchemy import select

    from .services import entitlements
    from .tenancy import set_org

    db = SessionLocal()
    try:
        running = list(db.scalars(select(SimState).where(SimState.running.is_(True)).order_by(SimState.id)))
        targets = [(st.id, st.speed) for st in running]
        db.rollback()
        for org_id, speed in targets:
            try:
                set_org(db, org_id)
                if entitlements.resolve(db, org_id)["access"] != "full":
                    db.rollback()
                    continue
                begin_immediate(db)   # take the write lock first so a concurrent writer can't cause a busy snapshot
                state = db.get(SimState, org_id)
                if state is None or not state.running:
                    db.rollback()
                    continue
                from .services.workspace_repair import fleet_size
                if fleet_size(db) == 0:          # nothing to simulate: never record empty snapshots
                    state.running = False
                    db.commit()
                    continue
                engine.step(db, max(1, state.speed))
                db.commit()
            except Exception:
                db.rollback()
                log.exception("Simulator tick failed for organisation %s", org_id)
    finally:
        db.close()


def _live_sla_tick() -> None:
    """SLA escalation for wall-clock sources (OCPP, operator API). Idempotent (escalated once).
    Imported historical records are not escalated. Simulated tickets are handled by the simulator."""
    from .clock import wall_now
    from .models import Ticket
    from .services.tickets import DONE, sla_sweep
    from sqlalchemy import select

    from .tenancy import set_org

    db = SessionLocal()
    try:
        orgs = sorted(set(db.scalars(select(Ticket.org_id).where(Ticket.source.in_(("ocpp", "operator_api")),
                                                                  Ticket.status.not_in(DONE)))))
        db.rollback()
        for org_id in orgs:                     # per organisation: escalation and re-assignment never cross tenants
            set_org(db, org_id)
            begin_immediate(db)
            for src in ("ocpp", "operator_api"):
                sla_sweep(db, src, wall_now())
            db.commit()
    except Exception:
        db.rollback()
        log.exception("SLA check failed")
    finally:
        db.close()


async def simulator_loop(stop: asyncio.Event) -> None:
    interval = max(0.1, get_settings().sim_tick_ms / 1000)
    last_sla = 0.0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        await run_in_threadpool(_tick)
        if loop.time() - last_sla >= 30:
            last_sla = loop.time()
            await run_in_threadpool(_live_sla_tick)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_database()
    stop = asyncio.Event()
    tasks = []
    if app.state.run_simulator_loop:
        from .services.operator_sync import effective_poll_seconds, poll_loop

        tasks.append(asyncio.create_task(simulator_loop(stop)))
        if effective_poll_seconds():
            log.info("Operator API polling every %s s.", effective_poll_seconds())
            tasks.append(asyncio.create_task(poll_loop(stop)))
    yield
    stop.set()
    for t in tasks:
        await t


def frontend_build_id() -> str:
    """Short content hash of the frontend files; changes whenever any of them changes."""
    h = hashlib.sha256()
    if FRONTEND_DIR.exists():
        for f in sorted(FRONTEND_DIR.rglob("*")):
            if f.is_file():
                h.update(f.relative_to(FRONTEND_DIR).as_posix().encode())
                h.update(f.read_bytes())
    return h.hexdigest()[:12]


def create_app(run_simulator_loop: bool = True) -> FastAPI:
    from fastapi import Depends

    from .api import (admin, analysis, auth, billing, historical_and_inventory, insights, integrations, meta, operations, org, planning, public_map,
                      validation)
    from .security.guard import guard
    from .security.ratelimit import RateLimiter
    from .services.entitlements import EntitlementError
    from .tenancy import CrossTenantWrite

    prod = get_settings().is_production
    app = FastAPI(title="ChargeSight", version=meta.VERSION, lifespan=lifespan, dependencies=[Depends(guard)],
                  docs_url=None if prod else "/docs", redoc_url=None if prod else "/redoc",
                  openapi_url=None if prod else "/openapi.json",
                  description="Explainable EV charger operations, repair prioritisation and charging-site investment intelligence (academic prototype).")
    app.state.run_simulator_loop = run_simulator_loop
    app.state.rate_limiter = RateLimiter()
    for r in (meta.router, operations.router, insights.router, analysis.router, public_map.router, planning.router, integrations.router,
              validation.router, auth.router, org.router, billing.router, admin.router, historical_and_inventory.router):
        app.include_router(r)

    @app.exception_handler(EntitlementError)
    async def _entitlement(request: Request, exc: EntitlementError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail()})

    @app.exception_handler(CrossTenantWrite)
    async def _cross_tenant(request: Request, exc: CrossTenantWrite):
        log.warning("Blocked cross-organisation write on %s", request.url.path)
        return JSONResponse(status_code=403, content={"detail": {"code": "forbidden", "message": "Not permitted."}})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # pragma: no cover - safety net
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error. See server log for details."})

    build = frontend_build_id()
    app.state.frontend_build = build

    @app.middleware("http")
    async def _security_and_cache(request: Request, call_next):
        path = request.url.path
        # Cross-site request defence in depth (besides SameSite cookies and the CSRF header): a browser
        # always sends Origin on cross-site POST/PUT/PATCH/DELETE; refuse it unless it is this server.
        if path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != "null":
                from urllib.parse import urlsplit

                if urlsplit(origin).netloc != request.headers.get("host", ""):
                    return JSONResponse(status_code=403, content={"detail": {"code": "bad_origin",
                                                                             "message": "Cross-site request refused."}})
        response = await call_next(request)
        # The SPA's ES modules are cached by browsers. Module URLs carry a content hash
        # (/static/<build>/...), and "no-cache" makes the browser revalidate the page itself,
        # so an updated server never runs yesterday's JavaScript.
        if not path.startswith("/api/") and not path.startswith("/static/"):
            if response.status_code not in (301, 302, 303, 307, 308):   # keep "no-store" on sign-in redirects
                response.headers["Cache-Control"] = "no-cache"
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        if path.startswith("/api/"):
            response.headers["X-ChargeSight-Build"] = build   # lets an open tab notice it is out of date
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if get_settings().secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    if FRONTEND_DIR.exists():
        app.mount(f"/static/{build}", StaticFiles(directory=FRONTEND_DIR), name="static_build")
        app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

        def _page(rel: str) -> HTMLResponse:
            html = (FRONTEND_DIR / rel).read_text(encoding="utf-8")
            html = html.replace('src="/static/', f'src="/static/{build}/').replace('href="/static/', f'href="/static/{build}/')
            html = html.replace("<head>", f'<head>\n  <meta name="cs-build" content="{build}">', 1)
            return HTMLResponse(html)

        @app.get("/app", include_in_schema=False)
        def console(request: Request):
            """The operations console. v10.1: without a valid session the server redirects to Sign In (the browser keeps
            the #fragment, so the intended view survives the round trip). The page is revalidated on every load, and the
            console re-checks the session when the browser restores it from the back/forward cache."""
            from fastapi.responses import RedirectResponse

            from .security.guard import SESSION_COOKIE
            from .services import accounts

            token = request.cookies.get(SESSION_COOKIE)
            valid = False
            if token:
                db = SessionLocal()
                try:
                    valid = accounts.load_session(db, token, touch=False) is not None
                    db.commit()
                finally:
                    db.close()
            if not valid:
                r = RedirectResponse("/signin?next=/app", status_code=303)
                r.headers["Cache-Control"] = "no-store"
                return r
            return _page("index.html")

        site_paths = ("/", "/features", "/pricing", "/enterprise", "/contact", "/signin", "/signup", "/demo", "/forgot-password",
                      "/reset-password", "/verify-email", "/accept-invite", "/terms", "/privacy", "/security")
        for sp in site_paths:
            app.add_api_route(sp, lambda: _page("site/index.html"), methods=["GET"], include_in_schema=False)

    return app


app = create_app()
