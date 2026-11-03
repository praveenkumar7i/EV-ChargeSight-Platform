"""Database engine and session handling (SQLAlchemy 2.0, SQLite by default)."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


log = logging.getLogger("chargesight.db")
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        db_path = url.replace("sqlite:///", "", 1)
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args=connect_args, future=True)

    if url.startswith("sqlite"):
        # pysqlite needs manual BEGIN for SAVEPOINT (per-event rollback during
        # ingestion) to work correctly; see the SQLAlchemy SQLite dialect docs.
        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_conn, _record):  # pragma: no cover - trivial
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # pragma: no cover - trivial
            # BEGIN IMMEDIATE takes the write lock up front (waiting up to busy_timeout), so a
            # read-then-write transaction cannot fail with SQLITE_BUSY when another process or
            # the simulator commits in between (a "busy snapshot", which busy_timeout cannot fix).
            immediate = conn.get_execution_options().get("sqlite_immediate")
            conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


def init_engine(url: str | None = None) -> Engine:
    """(Re)create the global engine. Tests call this with a temporary URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url or get_settings().database_url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def create_schema() -> None:
    from . import models  # noqa: F401  (register tables)

    backup = None
    if _needs_v9_migration():
        # back up BEFORE anything alters the schema (v9 adds tables/columns and rebuilds two tiny tables)
        backup = backup_sqlite("before-v9-saas-migration")
    if backup is None and pending_missing_columns():
        # v9.2+: any forward column addition is preceded by a verified backup
        backup = backup_sqlite_verified("before-v9-2-schema")
    Base.metadata.create_all(get_engine())
    migrate_v9_rebuild_keyed_tables()
    migrate_add_missing_columns()
    migrate_ticket_table_v8()
    migrate_v9_tenancy(backup)


def _needs_v9_migration() -> bool:
    """An existing pre-v9 database: it has the prototype tables but no organisations table."""
    from sqlalchemy import inspect

    eng = get_engine()
    if not str(eng.url).startswith("sqlite"):
        return False
    insp = inspect(eng)
    return insp.has_table("sites") and not insp.has_table("organisations")


PLATFORM_CONNECTORS = ("google_maps", "open_charge_map", "osm", "google_places")


def _rebuild_table(eng: Engine, table, row_fn) -> int:
    """SQLite table rebuild (new table, copy, drop, rename) in one transaction with foreign keys off.
    row_fn(old_row_dict) -> new_row_dict. Every row is copied; counts are checked."""
    from sqlalchemy.schema import CreateTable

    name = table.name
    ddl = str(CreateTable(table).compile(eng)).strip().replace(f"CREATE TABLE {name} ", f"CREATE TABLE {name}_v9_new ", 1)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE")
        try:
            cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{name}")').fetchall()]
            rows = [dict(zip(cols, r)) for r in cur.execute(f'SELECT * FROM "{name}"').fetchall()]
            cur.execute(ddl)
            new_cols = [c.name for c in table.columns]
            for r in rows:
                nr = row_fn(r)
                keys = [k for k in nr if k in new_cols]
                cur.execute(f'INSERT INTO "{name}_v9_new" ({", ".join(chr(34) + k + chr(34) for k in keys)}) '
                            f'VALUES ({", ".join("?" for _ in keys)})', [nr[k] for k in keys])
            after = cur.execute(f'SELECT COUNT(*) FROM "{name}_v9_new"').fetchone()[0]
            if after != len(rows):
                raise RuntimeError(f"{name} copy mismatch: {len(rows)} -> {after}")
            cur.execute(f'DROP TABLE "{name}"')
            cur.execute(f'ALTER TABLE "{name}_v9_new" RENAME TO "{name}"')
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()
    for idx in table.indexes:
        idx.create(eng, checkfirst=True)
    return after


def migrate_v9_rebuild_keyed_tables() -> dict:
    """connector_health and validation_tracks were keyed by name alone (one global row each). v9 keys
    them by (organisation, name). Rows of the existing prototype go to the legacy demo workspace
    (organisation 1); public-listing provider health stays platform-level (org_id NULL)."""
    from sqlalchemy import inspect

    from .models import ConnectorHealth, ValidationTrack

    eng = get_engine()
    out = {}
    if not str(eng.url).startswith("sqlite"):
        return out
    insp = inspect(eng)
    if insp.has_table("connector_health") and "org_id" not in {c["name"] for c in insp.get_columns("connector_health")}:
        out["connector_health"] = _rebuild_table(eng, ConnectorHealth.__table__, lambda r: {
            **r, "org_id": None if r.get("connector") in PLATFORM_CONNECTORS else 1})
    if insp.has_table("validation_tracks") and "org_id" not in {c["name"] for c in insp.get_columns("validation_tracks")}:
        out["validation_tracks"] = _rebuild_table(eng, ValidationTrack.__table__, lambda r: {**r, "org_id": 1})
    return out


TENANT_TABLES = ("sites", "chargers", "fleet_snapshots", "charger_readings", "events", "technicians", "tickets",
                 "ticket_history", "experiment_runs", "investment_sites", "financial_references", "ingestion_runs",
                 "interviews", "validation_datasets", "validation_tracks", "planning_candidates")


def migrate_v9_tenancy(backup=None) -> dict | None:
    """Give every existing prototype record an owner: the explicit system demo workspace
    ("ChargeSight Demo Workspace", organisation 1). Nothing is deleted or moved to a customer.
    One transaction; counts are logged and stored in the `v9_migration` setting."""
    import json
    import logging

    from sqlalchemy import inspect

    eng = get_engine()
    insp = inspect(eng)
    if not insp.has_table("organisations"):
        return None
    pending = {}
    with eng.connect() as conn:
        for t in TENANT_TABLES:
            if insp.has_table(t) and "org_id" in {c["name"] for c in insp.get_columns(t)}:
                n = conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{t}" WHERE org_id IS NULL').scalar() or 0
                if n:
                    pending[t] = n
    if not pending:
        return None
    log = logging.getLogger("chargesight.migrate")
    with eng.begin() as conn:
        conn.exec_driver_sql("INSERT OR IGNORE INTO organisations (id, name, code, org_type, country, is_system_demo, onboarding, "
                             "preferences, demo_workspace_ready, created_at, contact_phone, website, billing_address, tax_id, "
                             "primary_contact_name, primary_contact_email) VALUES (1, 'ChargeSight Demo Workspace', 'DEMO', 'other', "
                             "'India', 1, ?, '{}', 1, CURRENT_TIMESTAMP, '', '', '', '', '', '')",
                             (json.dumps({"completed": True, "data_mode": "demo", "note": "Existing prototype data (v9 migration)"}),))
        for t in pending:
            conn.exec_driver_sql(f'UPDATE "{t}" SET org_id = 1 WHERE org_id IS NULL')
        info = {"tables": pending, "backup": str(backup) if backup else None,
                "owner": "organisation 1 (ChargeSight Demo Workspace, system demo data)"}
        conn.exec_driver_sql("INSERT OR REPLACE INTO settings (key, value) VALUES ('v9_migration', ?)", (json.dumps(info),))
    log.warning("v9 migration: existing records assigned to the system demo workspace: %s (backup: %s)", pending, backup)
    return info


def _sqlite_path(eng: Engine) -> Path | None:
    url = str(eng.url)
    if not url.startswith("sqlite:///"):
        return None
    p = url.replace("sqlite:///", "", 1)
    return None if not p or p == ":memory:" else Path(p)


def backup_sqlite(label: str) -> Path | None:
    """Consistent online copy of the SQLite database into data/backups/ (never overwrites)."""
    import sqlite3
    from datetime import datetime as _dt

    eng = get_engine()
    src = _sqlite_path(eng)
    if src is None or not src.exists():
        return None
    dest_dir = src.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem}-{label}-{_dt.utcnow().strftime('%Y%m%dT%H%M%SZ')}.db"
    n = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}-{label}-{_dt.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{n}.db"
        n += 1
    with sqlite3.connect(str(src)) as a, sqlite3.connect(str(dest)) as b:
        a.backup(b)
        b.execute("PRAGMA journal_mode=DELETE")      # a single self-contained file (no -wal / -shm beside the backup)
    return dest


def migrate_ticket_table_v8() -> dict | None:
    """v8 widens the ticket status CHECK constraint (waiting_parts, waiting_access, closed) and the
    partial unique index. SQLite cannot alter a CHECK constraint, so the table is rebuilt following the
    documented procedure (new table, copy, drop, rename) with foreign keys off, after a full backup.
    Every row is copied; nothing is dropped. No-op on new databases and on PostgreSQL."""
    import logging

    from sqlalchemy.schema import CreateTable

    from .models import Ticket

    eng = get_engine()
    if not str(eng.url).startswith("sqlite"):
        return None
    with eng.connect() as conn:
        row = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='tickets'").first()
    if row is None or "'closed'" in (row[0] or ""):
        return None
    log = logging.getLogger("chargesight.migrate")
    backup = backup_sqlite("before-v8-ticket-migration")
    log.warning("Migrating the tickets table to the v8 schema (backup: %s)", backup)
    ddl = str(CreateTable(Ticket.__table__).compile(eng)).strip()
    ddl = ddl.replace("CREATE TABLE tickets ", "CREATE TABLE tickets_v8_new ", 1)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN IMMEDIATE")
        try:
            old_cols = [r[1] for r in cur.execute("PRAGMA table_info(tickets)").fetchall()]
            new_cols = {c.name for c in Ticket.__table__.columns}
            cols = [c for c in old_cols if c in new_cols]
            before = cur.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            cur.execute(ddl)
            collist = ", ".join(f'"{c}"' for c in cols)
            # rows from before v9 have no organisation yet: they belong to the system demo workspace (1)
            sellist = ", ".join('COALESCE("org_id", 1)' if c == "org_id" else f'"{c}"' for c in cols)
            cur.execute(f"INSERT INTO tickets_v8_new ({collist}) SELECT {sellist} FROM tickets")
            after = cur.execute("SELECT COUNT(*) FROM tickets_v8_new").fetchone()[0]
            if after != before:
                raise RuntimeError(f"ticket copy mismatch: {before} -> {after}")
            cur.execute("DROP TABLE tickets")
            cur.execute("ALTER TABLE tickets_v8_new RENAME TO tickets")
            problems = cur.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key check failed after migration: {problems[:5]}")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()
    for idx in Ticket.__table__.indexes:
        idx.create(eng, checkfirst=True)
    log.warning("Tickets table migrated: %d rows kept.", after)
    return {"rows": after, "backup": str(backup) if backup else None}


def verify_backup(dest: Path) -> dict:
    """Open a backup read-only and check that it is a readable, intact copy of the live database."""
    import sqlite3

    src = _sqlite_path(get_engine())
    with sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True) as b:
        check = b.execute("PRAGMA quick_check").fetchone()[0]
        tables = {r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True) as a:
        live = {r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ok = check == "ok" and live <= tables
    return {"path": str(dest), "quick_check": check, "tables": len(tables), "ok": ok, "bytes": dest.stat().st_size}


def backup_sqlite_verified(label: str) -> Path | None:
    """Back up, then verify the copy. Refuses to continue (raises) if the backup cannot be verified, so no
    migration or repair ever runs without a readable backup."""
    dest = backup_sqlite(label)
    if dest is None:
        return None
    info = verify_backup(dest)
    if not info["ok"]:
        raise RuntimeError(f"Database backup {dest} failed verification ({info['quick_check']}); nothing was changed.")
    log.warning("Database backup written and verified: %s (%s tables, %s bytes)", dest, info["tables"], info["bytes"])
    return dest


def pending_missing_columns() -> list[str]:
    """Columns present in the models but missing from an existing database file (read-only check)."""
    from sqlalchemy import inspect

    eng = get_engine()
    if _sqlite_path(eng) is None:
        return []
    insp = inspect(eng)
    out = []
    for table in Base.metadata.sorted_tables:
        if insp.has_table(table.name):
            have = {c["name"] for c in insp.get_columns(table.name)}
            out += [f"{table.name}.{c.name}" for c in table.columns if c.name not in have]
    return out


def migrate_add_missing_columns() -> list[str]:
    """Lightweight forward migration: add columns that exist in the models but
    not in an older database file (SQLite `create_all` never alters tables).
    Only ADD COLUMN is performed; existing data is never changed or dropped."""
    from sqlalchemy import inspect

    eng = get_engine()
    insp = inspect(eng)
    added = []
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl_type = col.type.compile(dialect=eng.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    v = col.default.arg
                    default = f" DEFAULT {int(v) if isinstance(v, bool) else repr(v)}" if isinstance(v, (int, float, bool)) else f" DEFAULT '{v}'"
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default}')
                added.append(f"{table.name}.{col.name}")
    return added


def begin_immediate(db: Session) -> None:
    """Start the session's next transaction as a write transaction (SQLite BEGIN IMMEDIATE).
    Call it when the session has no open transaction, right before a short read-modify-write
    block. On other databases it is a no-op."""
    if db.in_transaction():
        db.commit()
    db.connection(execution_options={"sqlite_immediate": True})


def is_db_busy(exc: BaseException) -> bool:
    msg = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in msg or "database is busy" in msg


def SessionLocal() -> Session:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency. The session is bound to the organisation that the auth guard resolved for
    this request (tenancy.set_org); anonymous requests get a session that sees no tenant data."""
    from .tenancy import NO_ORG, set_org

    session = SessionLocal()
    set_org(session, getattr(request.state, "org_id", None) or NO_ORG)
    try:
        yield session
    finally:
        session.close()


def get_system_db() -> Iterator[Session]:
    """Unfiltered session for platform-administrator endpoints (the guard checks the admin flag)."""
    from .tenancy import set_system

    session = SessionLocal()
    set_system(session, platform=True)
    try:
        yield session
    finally:
        session.close()
