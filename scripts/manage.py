"""ChargeSight administration commands (run from the project root with the virtual environment active).

  python scripts/manage.py create-admin --email you@example.com --name "Your Name"
      Creates a PLATFORM ADMINISTRATOR and makes them owner of the system "ChargeSight Demo Workspace"
      (the prototype data that existed before v9). The password is typed interactively (never an argument,
      never stored in source code or logs).
  python scripts/manage.py grant --email person@example.com --org 1 --role viewer
  python scripts/manage.py reset-password --email person@example.com
  python scripts/manage.py list-users
  python scripts/manage.py list-orgs

Nothing here deletes data.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def _init():
    from app.db import create_schema, session_scope
    from app.services.seed import seed_reference_data

    create_schema()
    with session_scope() as db:
        seed_reference_data(db)


def _password(prompt="Password: ") -> str:
    from app.security.passwords import password_problems

    while True:
        p1 = getpass.getpass(prompt)
        p2 = getpass.getpass("Repeat password: ")
        if p1 != p2:
            print("Passwords do not match.")
            continue
        probs = password_problems(p1)
        if probs:
            print(" ".join(probs))
            continue
        return p1


def create_admin(a) -> None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models_saas import User
    from app.services import accounts, audit

    _init()
    with session_scope() as db:
        u = db.scalar(select(User).where(User.email == accounts.normalise_email(a.email)))
        if u is None:
            u = accounts.create_user_direct(db, email=a.email, full_name=a.name, password=_password(), platform_admin=True)
            print(f"Created platform administrator {u.email}.")
        else:
            u.is_platform_admin = True
            print(f"{u.email} already existed; granted platform administrator.")
        accounts.grant(db, u, 1, "owner")
        audit.record(db, "cli.create_admin", org_id=1, actor_label="manage.py", target_type="user", target_id=u.id)
    print("They own the system 'ChargeSight Demo Workspace' (existing prototype data). Sign in at http://127.0.0.1:8000/signin")


def grant(a) -> None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models_saas import Organisation, User
    from app.services import accounts, audit
    from app.services.orgs import ROLE_LABELS

    _init()
    if a.role not in ROLE_LABELS:
        sys.exit(f"Role must be one of: {', '.join(ROLE_LABELS)}")
    with session_scope() as db:
        u = db.scalar(select(User).where(User.email == accounts.normalise_email(a.email)))
        if u is None:
            sys.exit("No such user (they must sign up first, or use create-admin).")
        if db.get(Organisation, a.org) is None:
            sys.exit("No such organisation.")
        accounts.grant(db, u, a.org, a.role)
        audit.record(db, "cli.grant", org_id=a.org, actor_label="manage.py", target_type="user", target_id=u.id, data={"role": a.role})
    print(f"{a.email} is now {a.role} of organisation {a.org}.")


def reset_password(a) -> None:
    from sqlalchemy import select

    from app.clock import wall_now
    from app.db import session_scope
    from app.models_saas import User
    from app.security.passwords import hash_password
    from app.services import accounts, audit

    _init()
    with session_scope() as db:
        u = db.scalar(select(User).where(User.email == accounts.normalise_email(a.email)))
        if u is None:
            sys.exit("No such user.")
        u.password_hash = hash_password(_password("New password: "))
        u.password_changed_at = wall_now()
        accounts.revoke_all(db, u.id)
        audit.record(db, "cli.reset_password", actor_label="manage.py", target_type="user", target_id=u.id)
    print("Password changed; existing sessions were signed out.")


def list_users(a) -> None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models_saas import Membership, User

    _init()
    with session_scope() as db:
        for u in db.scalars(select(User).order_by(User.id)):
            ms = db.scalars(select(Membership).where(Membership.user_id == u.id, Membership.status == "active"))
            print(f"{u.id:4} {u.email:40} {'PLATFORM-ADMIN ' if u.is_platform_admin else ''}"
                  + ", ".join(f"org {m.org_id}:{m.role}" for m in ms))


def list_orgs(a) -> None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models_saas import Organisation
    from app.services import billing

    _init()
    with session_scope() as db:
        for o in db.scalars(select(Organisation).order_by(Organisation.id)):
            sub = billing.current(db, o.id)
            print(f"{o.id:4} {o.code:10} {o.name:40} {sub.plan_key:10} {sub.state}")


def main() -> None:
    p = argparse.ArgumentParser(description="ChargeSight administration")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-admin")
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)
    c.set_defaults(fn=create_admin)
    g = sub.add_parser("grant")
    g.add_argument("--email", required=True)
    g.add_argument("--org", type=int, required=True)
    g.add_argument("--role", required=True)
    g.set_defaults(fn=grant)
    r = sub.add_parser("reset-password")
    r.add_argument("--email", required=True)
    r.set_defaults(fn=reset_password)
    sub.add_parser("list-users").set_defaults(fn=list_users)
    sub.add_parser("list-orgs").set_defaults(fn=list_orgs)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
