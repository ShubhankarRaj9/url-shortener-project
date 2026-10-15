"""Integration tests: they run against a real PostgreSQL and Redis.

    TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost:5432/urlshort_test \
    TEST_REDIS_URL=redis://localhost:6379/15 pytest
"""

import os
import subprocess
import sys
from pathlib import Path

DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/urlshort_test")
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

os.environ.update(
    ENVIRONMENT="test",
    DATABASE_URL=DB_URL,
    REDIS_URL=REDIS_URL,
    SECRET_KEY="test-secret-key-that-is-long-enough-1234567890",
    EMAIL_BACKEND="memory",
    BCRYPT_ROUNDS="4",
    PUBLIC_BASE_URL="https://sho.rt",
    ADMIN_EMAIL="admin@example.com",
    ADMIN_PASSWORD="admin-password-123",
    CLICK_FLUSH_INTERVAL_SECONDS="3600",  # tests flush explicitly
    LOG_JSON="false",
    LOG_LEVEL="WARNING",
)

import pytest  # noqa: E402
import redis as redis_sync  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import emailer  # noqa: E402
from app.main import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    run = lambda *a: subprocess.run([sys.executable, "-m", "alembic", *a], cwd=ROOT, env=env, check=True)  # noqa: E731
    run("downgrade", "base")
    run("upgrade", "head")
    run("check")  # fails if models and migrations ever drift apart


@pytest.fixture
def client():
    redis_sync.Redis.from_url(REDIS_URL).flushdb()
    emailer.OUTBOX.clear()
    app = create_app()
    with TestClient(app, base_url="http://testserver") as c:
        # start every test from an empty users table, then let the seeder run again
        from sqlalchemy import text

        async def reset():
            async with app.state.engine.begin() as conn:
                await conn.execute(text("TRUNCATE users, links, auth_tokens RESTART IDENTITY CASCADE"))
            from app.config import get_settings
            from app.services.seed import seed_admin

            await seed_admin(get_settings(), app.state.sessionmaker)

        c.portal.call(reset)
        c.app_ref = app
        yield c


# ---------------------------------------------------------------- helpers
PASSWORD = "correct-horse-battery"


def register_and_verify(client, email="user@example.com", password=PASSWORD):
    r = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 202, r.text
    mail = emailer.OUTBOX[-1]
    assert mail["to"] == email
    token = _token(mail)
    r = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200, r.text


def _token(mail) -> str:
    line = [ln for ln in mail["body"].splitlines() if "token=" in ln][0]
    return line.split("token=")[1].strip()


def login(client, email="user@example.com", password=PASSWORD):
    r = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    return r


def auth_headers(client, email="user@example.com", password=PASSWORD):
    r = login(client, email, password)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def user_headers(client):
    register_and_verify(client)
    return auth_headers(client)


@pytest.fixture
def admin_headers(client):
    return auth_headers(client, "admin@example.com", "admin-password-123")


@pytest.fixture
def flush(client):
    from app.services.clicks import flush_clicks

    def _flush():
        app = client.app_ref
        return client.portal.call(flush_clicks, app.state.redis, app.state.sessionmaker)

    return _flush
