import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# Never read the developer's real .env during tests.
os.environ.setdefault("GOOGLE_MAPS_BROWSER_KEY", "")
os.environ.setdefault("OCM_API_KEY", "")
os.environ.setdefault("OPERATOR_API_BASE_URL", "")
os.environ.setdefault("OPERATOR_API_KEY", "")
os.environ.setdefault("ADMIN_TOKEN", "")
os.environ.setdefault("GOOGLE_PLACES_API_KEY", "")
os.environ["OSM_ENABLED"] = "false"       # never call the public Overpass service from tests
os.environ["DEMO_AUTOSEED"] = "false"     # tests opt in to the demo dataset explicitly
os.environ["DEV_OUTBOX_ENABLED"] = "false"   # tests that need the development outbox enable it explicitly
os.environ.setdefault("AUTH_SIGNUP_LIMIT", "1000")
os.environ.setdefault("AUTH_SIGNIN_LIMIT", "1000")
os.environ.setdefault("AUTH_SIGNIN_IP_LIMIT", "1000")
os.environ.setdefault("PUBLIC_FORM_LIMIT", "1000")

LEGACY_OWNER = ("owner@demo.chargesight.test", "Legacy-Ws-Pass-2026!", "Test Owner")


def ensure_user(email, password, name, org_id=None, role="owner", platform_admin=False):
    """Create (once) a user, optionally with a membership. Uses the service layer (hashed passwords)."""
    from sqlalchemy import select

    from app import db as D
    from app.models_saas import User
    from app.services import accounts

    with D.session_scope() as s:
        u = s.scalar(select(User).where(User.email == email))
        if u is None:
            u = accounts.create_user_direct(s, email=email, full_name=name, password=password, platform_admin=platform_admin)
        if org_id is not None:
            accounts.grant(s, u, org_id, role)
        return u.id


def sign_in(c, email, password):
    r = c.post("/api/auth/signin", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r.json()


def authed(c, user=LEGACY_OWNER, org_id=1, role="owner"):
    """Sign a TestClient in as a member of an organisation. Default: the developer account that
    scripts/manage.py create-admin sets up - platform administrator and owner of the legacy demo workspace."""
    ensure_user(user[0], user[1], user[2], org_id=org_id, role=role, platform_admin=user is LEGACY_OWNER)
    sign_in(c, user[0], user[1])
    return c


@pytest.fixture
def settings_env(monkeypatch):
    """Set environment variables and refresh the cached settings."""
    from app.config import get_settings

    def _set(**kv):
        for k, v in kv.items():
            monkeypatch.setenv(k.upper(), str(v))
        get_settings.cache_clear()
        return get_settings()

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    from app.config import get_settings

    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


@pytest.fixture
def db(db_url):
    from app import db as D
    from app.services.seed import seed_reference_data

    D.init_engine(db_url)
    D.create_schema()
    s = D.SessionLocal()
    seed_reference_data(s)
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client(db_url):
    from fastapi.testclient import TestClient

    from app import db as D
    from app.main import create_app

    D.init_engine(db_url)
    app = create_app(run_simulator_loop=False)
    with TestClient(app) as c:
        authed(c)
        yield c


@pytest.fixture
def anon_client(db_url):
    """A client with no session (for authentication and public-page tests)."""
    from fastapi.testclient import TestClient

    from app import db as D
    from app.main import create_app

    D.init_engine(db_url)
    with TestClient(create_app(run_simulator_loop=False)) as c:
        yield c


@pytest.fixture(scope="session")
def demo_db_template(tmp_path_factory):
    """Build the synthetic demo database once per test session (it takes a few seconds)."""
    from app import db as D
    from app.config import get_settings
    from app.services.demo_seed import build_demo
    from app.services.seed import seed_reference_data

    path = tmp_path_factory.mktemp("demo") / "demo.db"
    url = f"sqlite:///{path.as_posix()}"
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    D.init_engine(url)
    D.create_schema()
    with D.session_scope() as s:
        seed_reference_data(s)
    with D.session_scope() as s:
        build_demo(s)
    D.get_engine().dispose()
    os.environ.pop("DATABASE_URL", None)
    get_settings.cache_clear()
    return path


@pytest.fixture
def demo_url(demo_db_template, tmp_path, monkeypatch):
    import shutil

    from app.config import get_settings

    target = tmp_path / "demo.db"
    shutil.copy(demo_db_template, target)
    for suffix in ("-wal", "-shm"):
        src = Path(str(demo_db_template) + suffix)
        if src.exists():
            shutil.copy(src, str(target) + suffix)
    url = f"sqlite:///{target.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


@pytest.fixture
def demo_client(demo_url):
    from fastapi.testclient import TestClient

    from app import db as D
    from app.main import create_app

    D.init_engine(demo_url)
    app = create_app(run_simulator_loop=False)
    with TestClient(app) as c:
        authed(c)
        yield c
