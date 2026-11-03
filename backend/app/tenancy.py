"""Multi-tenancy: every operational record belongs to exactly one organisation.

How isolation works
-------------------
* Tenant-owned tables carry ``org_id`` (``TenantMixin``).
* A database session used for a web request is bound to the signed-in user's organisation
  (``set_org``). A ``do_orm_execute`` hook then adds ``org_id = <that organisation>`` to every ORM
  SELECT / UPDATE / DELETE, including ``Session.get``, joins, sub-queries and relationship loads
  (SQLAlchemy ``with_loader_criteria``). A request for another organisation's ticket therefore
  finds nothing and returns 404, whatever ID is put in the URL.
* Anonymous request sessions are bound to ``NO_ORG`` (-1): they see no tenant rows and cannot
  write any.
* New rows get the session's organisation in ``before_flush``. A row's ``org_id`` can never be
  changed afterwards.
* Sessions without a binding are *system* sessions (start-up seeding, migrations, CLI, tests of
  the existing prototype). Rows they create inherit the organisation of their parent charger,
  site or ticket, otherwise the legacy demo workspace (organisation 1).
* Platform-administrator endpoints use an explicit system session.

Public Open Charge Map / OpenStreetMap listings are shared reference data (no ``org_id``).
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, event, select
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, declared_attr, mapped_column, with_loader_criteria

LEGACY_ORG_ID = 1          # "ChargeSight Demo Workspace": the prototype data that existed before v9
NO_ORG = -1                # anonymous request sessions: see nothing, write nothing
ALL_TENANTS = "cs_all_tenants"   # execution option: bypass the tenant filter (platform-level reads only)


class TenantMixin:
    @declared_attr
    def org_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey("organisations.id"), index=True, nullable=False)


class CrossTenantWrite(PermissionError):
    pass


# ---------------------------------------------------------------- session binding
def set_org(db: Session, org_id: int) -> Session:
    db.info["org_id"] = int(org_id)
    db.info.pop("system", None)
    db.info.pop("org_code", None)
    return db


def set_system(db: Session, *, platform: bool = False) -> Session:
    db.info.pop("org_id", None)
    db.info.pop("org_code", None)
    db.info["system"] = True
    if platform:
        db.info["platform"] = True
    return db


def current_org(db: Session) -> int | None:
    v = db.info.get("org_id")
    return None if v is None or v == NO_ORG else int(v)


def effective_org(db: Session) -> int:
    """Organisation whose settings / simulator a call works on (system sessions: the legacy workspace)."""
    return current_org(db) or LEGACY_ORG_ID


def org_code(db: Session) -> str:
    code = db.info.get("org_code")
    if code is None:
        from .models_saas import Organisation

        org = db.get(Organisation, effective_org(db))
        code = org.code if org is not None else "DEMO"
        db.info["org_code"] = code
    return code


def nid(db: Session, raw: str) -> str:
    """Namespaced identifier for globally unique keys (charger ids, site codes, technician ids).
    The legacy workspace keeps its original ids (SIM-HSR-01, TECH-01, ...); other organisations get
    their code as a prefix (e.g. GRID7-SIM-HSR-01) so two organisations can never collide."""
    if effective_org(db) == LEGACY_ORG_ID:
        return raw
    prefix = f"{org_code(db)}-"
    return raw if raw.startswith(prefix) else prefix + raw


def sim_state(db: Session):
    """The simulator state of the session's organisation (one row per organisation, id = org id)."""
    from .models import SimState

    return db.get(SimState, effective_org(db))


# ---------------------------------------------------------------- ORM hooks
@event.listens_for(Session, "do_orm_execute")
def _tenant_filter(state: ORMExecuteState) -> None:
    org = state.session.info.get("org_id")
    if org is None or state.execution_options.get(ALL_TENANTS, False):
        return
    if (state.is_select and not state.is_column_load and not state.is_relationship_load) or state.is_update or state.is_delete:
        state.statement = state.statement.options(
            with_loader_criteria(TenantMixin, lambda cls: cls.org_id == org, include_aliases=True))


_PARENT_LINKS = (("charger_id", "Charger"), ("ticket_id", "Ticket"), ("site_id", "Site"),
                 ("investment_site_id", "InvestmentSite"))


def _parent_org(db: Session, obj) -> int | None:
    from . import models

    with db.no_autoflush:
        for attr, model_name in _PARENT_LINKS:
            key = getattr(obj, attr, None)
            if key is None:
                continue
            model = getattr(models, model_name)
            parent = next((o for o in db.new if isinstance(o, model) and getattr(o, "id", None) == key), None)
            if parent is None:
                parent = db.get(model, key, execution_options={ALL_TENANTS: True})
            if parent is not None and getattr(parent, "org_id", None):
                return parent.org_id
    return None


@event.listens_for(Session, "before_flush")
def _assign_org(db: Session, _ctx, _instances) -> None:
    ctx = db.info.get("org_id")
    for obj in list(db.new):
        if not isinstance(obj, TenantMixin):
            continue
        if ctx == NO_ORG:
            raise CrossTenantWrite("This request is not bound to an organisation.")
        if ctx is not None:
            if obj.org_id is None:
                obj.org_id = int(ctx)
            elif obj.org_id != int(ctx):
                raise CrossTenantWrite("A record cannot be created for another organisation.")
            continue
        if obj.org_id is not None:
            continue
        obj.org_id = _parent_org(db, obj) or LEGACY_ORG_ID
    for obj in list(db.dirty):
        if not isinstance(obj, TenantMixin):
            continue
        from sqlalchemy import inspect as sa_inspect

        hist = sa_inspect(obj).attrs.org_id.history
        if hist.deleted and hist.deleted[0] is not None and hist.added and hist.added[0] != hist.deleted[0]:
            raise CrossTenantWrite("A record's organisation cannot be changed.")


def tenant_count(db: Session, model, *where) -> int:
    from sqlalchemy import func

    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


class as_org:
    """Context manager: bind a session to an organisation for a block, then restore the previous binding.
    `as_org(db)` with no id binds a system session to the legacy workspace and leaves a bound session as is."""

    def __init__(self, db: Session, org_id: int | None = None):
        self.db = db
        self.org_id = org_id

    def __enter__(self) -> Session:
        self._saved = {k: self.db.info.get(k) for k in ("org_id", "system", "org_code")}
        target = self.org_id if self.org_id is not None else (current_org(self.db) or LEGACY_ORG_ID)
        if self.db.info.get("org_id") != target:
            set_org(self.db, target)
        return self.db

    def __exit__(self, *exc) -> None:
        for k, v in self._saved.items():
            if v is None:
                self.db.info.pop(k, None)
            else:
                self.db.info[k] = v
