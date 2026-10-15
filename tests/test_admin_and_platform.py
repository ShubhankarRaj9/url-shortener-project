from conftest import register_and_verify, auth_headers
from sqlalchemy import text


def test_admin_is_seeded_once(client, admin_headers):
    me = client.get("/api/v1/users/me", headers=admin_headers).json()
    assert me["role"] == "admin" and me["is_verified"] is True

    from app.config import get_settings
    from app.services.seed import seed_admin

    app = client.app_ref
    assert client.portal.call(seed_admin, get_settings(), app.state.sessionmaker) is False  # users exist now
    users = client.get("/api/v1/admin/users", headers=admin_headers).json()
    assert users["total"] == 1


def test_admin_endpoints_forbidden_for_normal_users(client, user_headers):
    assert client.get("/api/v1/admin/users", headers=user_headers).status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 401
    me = client.get("/api/v1/users/me", headers=user_headers).json()
    assert client.delete(f"/api/v1/admin/users/{me['id']}", headers=user_headers).status_code == 403


def test_admin_list_search_and_delete_user(client, admin_headers):
    register_and_verify(client, "alice@example.com")
    register_and_verify(client, "bob@example.com")
    alice = auth_headers(client, "alice@example.com")
    link = client.post("/api/v1/links", headers=alice, json={"url": "https://example.org", "alias": "alice-link"}).json()
    assert client.get("/alice-link", follow_redirects=False).status_code == 302  # cached

    listing = client.get("/api/v1/admin/users", headers=admin_headers).json()
    assert listing["total"] == 3
    assert {u["email"] for u in listing["items"]} == {"admin@example.com", "alice@example.com", "bob@example.com"}
    assert all("password" not in u for u in listing["items"])
    found = client.get("/api/v1/admin/users?q=ALI", headers=admin_headers).json()
    assert [u["email"] for u in found["items"]] == ["alice@example.com"]
    assert client.get("/api/v1/admin/users?q=%25", headers=admin_headers).json()["total"] == 0  # LIKE wildcards escaped

    alice_id = found["items"][0]["id"]
    assert client.delete(f"/api/v1/admin/users/{alice_id}", headers=admin_headers).status_code == 204
    assert client.get(f"/api/v1/admin/users/{alice_id}", headers=admin_headers).status_code == 404
    assert client.get("/alice-link", follow_redirects=False).status_code == 404  # links + cache gone
    assert client.get("/api/v1/users/me", headers=alice).status_code == 401  # their token no longer works
    all_links = client.get("/api/v1/admin/links", headers=admin_headers).json()
    assert all(item["id"] != link["id"] for item in all_links["items"])


def test_admin_cannot_delete_self(client, admin_headers):
    me = client.get("/api/v1/users/me", headers=admin_headers).json()
    assert client.delete(f"/api/v1/admin/users/{me['id']}", headers=admin_headers).status_code == 409


def test_cannot_delete_last_admin(client, admin_headers):
    register_and_verify(client, "second@example.com")
    uid = client.get("/api/v1/users/me", headers=auth_headers(client, "second@example.com")).json()["id"]

    async def promote():
        async with client.app_ref.state.engine.begin() as c:
            await c.execute(text("update users set role='admin' where email='second@example.com'"))

    client.portal.call(promote)
    second = auth_headers(client, "second@example.com")
    first_id = client.get("/api/v1/users/me", headers=admin_headers).json()["id"]
    assert client.delete(f"/api/v1/admin/users/{first_id}", headers=second).status_code == 204  # fine: another admin remains
    assert client.delete(f"/api/v1/admin/users/{uid}", headers=second).status_code == 409


def test_admin_can_delete_any_link(client, user_headers, admin_headers):
    link = client.post("/api/v1/links", headers=user_headers, json={"url": "https://example.org"}).json()
    assert client.delete(f"/api/v1/admin/links/{link['id']}", headers=admin_headers).status_code == 204
    assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 404


# ------------------------------------------------------------- platform
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "redis": "ok"}
    assert client.get("/health/live").json()["status"] == "ok"


def test_health_reports_redis_outage(client, monkeypatch):
    async def boom():
        raise ConnectionError("redis down: secret-host:6379")

    monkeypatch.setattr(client.app_ref.state.redis, "ping", boom)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": "ok", "redis": "error"}
    assert "secret-host" not in r.text  # no internals leak
    assert client.get("/health/live").status_code == 200


def test_redirects_and_clicks_survive_redis_outage(client, user_headers, monkeypatch):
    link = client.post("/api/v1/links", headers=user_headers, json={"url": "https://example.org/x"}).json()
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def boom(*a, **k):
        raise RedisConnectionError("down")

    for name in ("get", "set", "xadd"):
        monkeypatch.setattr(client.app_ref.state.redis, name, boom)
    r = client.get(f"/{link['code']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://example.org/x"
    # the click went straight to PostgreSQL, so it is counted without any flush
    assert client.get(f"/api/v1/links/{link['id']}", headers=user_headers).json()["click_count"] == 1


def test_metrics_internal_only(client):
    client.get("/health")
    direct = client.get("/metrics")
    assert direct.status_code == 200
    assert "http_requests_total" in direct.text and "shortener_redirects_total" in direct.text
    assert 'route="/health"' in direct.text
    proxied = client.get("/metrics", headers={"X-Forwarded-For": "1.2.3.4"})
    assert proxied.status_code == 404


def test_swagger_and_openapi(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/api/v1/auth/login" in spec["paths"] and "/api/v1/admin/users" in spec["paths"]
    assert "/metrics" not in spec["paths"]
    assert "OAuth2PasswordBearer" in spec["components"]["securitySchemes"]


def test_request_id_and_headers(client):
    r = client.get("/health/live", headers={"X-Request-ID": "abc12345-trace"})
    assert r.headers["x-request-id"] == "abc12345-trace"
    assert r.headers["x-content-type-options"] == "nosniff"
    r = client.get("/health/live", headers={"X-Request-ID": "bad id with spaces\t"})
    assert r.headers["x-request-id"] != "bad id with spaces\t" and len(r.headers["x-request-id"]) == 32
    assert client.get("/api/v1/links").headers["cache-control"] == "no-store"


def test_structured_json_logs(client, capsys):
    import json
    import logging

    from app.logging import configure_logging

    configure_logging("INFO", True, service="url-shortener", environment="test")
    capsys.readouterr()
    client.get("/nothing-here", headers={"User-Agent": "pytest"})
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    access = [ln for ln in lines if ln.get("event") == "http.request"][0]
    assert access["status"] == 404 and access["method"] == "GET" and access["route"] == "/{code}"
    assert access["request_id"] and access["service"] == "url-shortener" and "timestamp" in access
    logging.getLogger().handlers.clear()


def test_secrets_never_logged(client, capsys):
    from app.logging import configure_logging

    configure_logging("INFO", True)
    capsys.readouterr()
    client.post("/api/v1/auth/register", json={"email": "leak@example.com", "password": "super-secret-pass-1"})
    out = capsys.readouterr().out
    assert "super-secret-pass-1" not in out
