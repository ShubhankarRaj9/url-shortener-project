import jwt
from conftest import PASSWORD, _token, auth_headers, login, register_and_verify

from app import emailer


def test_register_verify_login_flow(client):
    r = client.post("/api/v1/auth/register", json={"email": "New@Example.com", "password": PASSWORD, "full_name": "N"})
    assert r.status_code == 202

    # cannot log in before verifying
    assert login(client, "new@example.com").status_code == 403

    mail = emailer.OUTBOX[-1]
    assert mail["to"] == "new@example.com"  # normalised to lower case
    token = _token(mail)
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200
    # tokens are single use
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 400

    r = login(client, "NEW@example.com")
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 1800

    me = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "new@example.com"
    assert me.json()["role"] == "user" and me.json()["is_verified"] is True
    assert "password" not in me.text


def test_register_does_not_reveal_existing_accounts(client):
    register_and_verify(client)
    sent = len(emailer.OUTBOX)
    r1 = client.post("/api/v1/auth/register", json={"email": "user@example.com", "password": PASSWORD})
    r2 = client.post("/api/v1/auth/register", json={"email": "other@example.com", "password": PASSWORD})
    assert r1.status_code == r2.status_code == 202
    assert r1.json() == r2.json()
    assert len(emailer.OUTBOX) == sent + 1  # only the genuinely new address got mail


def test_password_policy(client):
    for pw in ["short", "x" * 73, " " * 12]:
        r = client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": pw})
        assert r.status_code == 422, pw
    assert client.post("/api/v1/auth/register", json={"email": "not-an-email", "password": PASSWORD}).status_code == 422


def test_login_failures_and_lockout(client):
    register_and_verify(client)
    assert login(client, password="wrong-password-1").status_code == 401
    assert login(client, "nobody@example.com").status_code == 401  # same answer for unknown users
    for _ in range(4):
        login(client, password="wrong-password-1")
    locked = login(client, password=PASSWORD)  # even the right password is refused while locked
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0


def test_resend_verification(client):
    client.post("/api/v1/auth/register", json={"email": "r@example.com", "password": PASSWORD})
    first = _token(emailer.OUTBOX[-1])
    r = client.post("/api/v1/auth/resend-verification", json={"email": "r@example.com"})
    assert r.status_code == 202
    new = _token(emailer.OUTBOX[-1])
    assert new != first
    assert client.post("/api/v1/auth/verify-email", json={"token": first}).status_code == 400  # superseded
    assert client.post("/api/v1/auth/verify-email", json={"token": new}).status_code == 200


def test_password_reset_flow_invalidates_old_sessions(client):
    register_and_verify(client)
    old = auth_headers(client)
    assert client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"}).status_code == 202
    n = len(emailer.OUTBOX)
    assert client.post("/api/v1/auth/forgot-password", json={"email": "user@example.com"}).status_code == 202
    assert len(emailer.OUTBOX) == n + 1
    token = _token(emailer.OUTBOX[-1])

    assert client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "short"}).status_code == 422
    r = client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "brand-new-password"})
    assert r.status_code == 200
    assert client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "another-password-1"}).status_code == 400

    assert client.get("/api/v1/users/me", headers=old).status_code == 401  # old JWT revoked
    assert login(client, password=PASSWORD).status_code == 401
    assert login(client, password="brand-new-password").status_code == 200


def test_change_password(client, user_headers):
    r = client.post(
        "/api/v1/auth/change-password",
        headers=user_headers,
        json={"current_password": "nope-nope-nope", "new_password": "a-brand-new-password"},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/v1/auth/change-password",
        headers=user_headers,
        json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert r.status_code == 200
    assert client.get("/api/v1/users/me", headers=user_headers).status_code == 401


def test_bad_tokens_rejected(client, user_headers):
    from app.config import get_settings

    assert client.get("/api/v1/users/me").status_code == 401
    assert client.get("/api/v1/users/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    forged = jwt.encode({"sub": "x", "ver": 0, "iss": "url-shortener"}, "k" * 40, algorithm="HS256")
    assert client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    none_alg = jwt.encode({"sub": "x"}, None, algorithm="none")
    assert client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {none_alg}"}).status_code == 401
    assert get_settings().jwt_algorithm == "HS256"


def test_passwords_are_stored_as_bcrypt(client):
    import asyncio  # noqa: F401

    from sqlalchemy import text

    register_and_verify(client)

    async def fetch():
        async with client.app_ref.state.engine.connect() as c:
            return (await c.execute(text("select password_hash from users where email='user@example.com'"))).scalar()

    stored = client.portal.call(fetch)
    assert stored.startswith("$2b$") and PASSWORD not in stored
