from conftest import register_and_verify, auth_headers


def create(client, headers, url="https://example.org/a/very/long/path?x=1", alias=None):
    return client.post("/api/v1/links", headers=headers, json={"url": url, "alias": alias})


def test_create_generated_and_redirect(client, user_headers):
    r = create(client, user_headers)
    assert r.status_code == 201, r.text
    link = r.json()
    assert len(link["code"]) == 7 and link["short_url"] == f"https://sho.rt/{link['code']}"
    assert link["is_custom"] is False and link["click_count"] == 0

    redirect = client.get(f"/{link['code']}", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "https://example.org/a/very/long/path?x=1"
    assert client.head(f"/{link['code']}", follow_redirects=False).status_code == 302


def test_custom_alias(client, user_headers):
    r = create(client, user_headers, alias="my-Link_1")
    assert r.status_code == 201 and r.json()["code"] == "my-Link_1" and r.json()["is_custom"] is True
    assert client.get("/my-Link_1", follow_redirects=False).status_code == 302
    assert create(client, user_headers, alias="my-Link_1").status_code == 409  # taken
    for bad in ["ab", "has space", "x" * 33, "ünï", "a/b"]:
        assert create(client, user_headers, alias=bad).status_code == 422, bad
    for reserved in ["docs", "Health", "API", "metrics", "admin"]:
        assert create(client, user_headers, alias=reserved).status_code == 409, reserved


def test_url_validation(client, user_headers):
    for bad in [
        "javascript:alert(1)",
        "ftp://example.org/x",
        "//example.org",
        "https://user:pw@example.org/",
        "https://exa mple.org",
        "https://example.org:99999/",
        "https://" + "a" * 2100 + ".com",
    ]:
        assert create(client, user_headers, url=bad).status_code == 422, bad
    # redirect loops through the shortener itself are refused
    assert create(client, user_headers, url="https://sho.rt/abc").status_code == 422


def test_unknown_and_invalid_codes_404(client):
    assert client.get("/nope123", follow_redirects=False).status_code == 404
    assert client.get("/nope123", follow_redirects=False).status_code == 404  # negative cache path
    assert client.get("/a", follow_redirects=False).status_code == 404  # too short to be valid
    assert client.get("/not.a.code", follow_redirects=False).status_code == 404


def test_new_link_beats_negative_cache(client, user_headers):
    assert client.get("/fresh-one", follow_redirects=False).status_code == 404
    create(client, user_headers, alias="fresh-one")
    assert client.get("/fresh-one", follow_redirects=False).status_code == 302


def test_click_batching(client, user_headers, flush):
    link = create(client, user_headers).json()
    for _ in range(7):
        client.get(f"/{link['code']}", follow_redirects=False)
    client.head(f"/{link['code']}", follow_redirects=False)  # HEAD is not a click

    # still buffered in Redis, Postgres untouched
    assert client.get(f"/api/v1/links/{link['id']}", headers=user_headers).json()["click_count"] == 0
    assert flush() == 7
    got = client.get(f"/api/v1/links/{link['id']}", headers=user_headers).json()
    assert got["click_count"] == 7 and got["last_clicked_at"] is not None
    assert flush() == 0

    client.get(f"/{link['code']}", follow_redirects=False)
    assert flush() == 1
    assert client.get(f"/api/v1/links/{link['id']}", headers=user_headers).json()["click_count"] == 8


def test_list_only_own_links_and_pagination(client, user_headers):
    for i in range(3):
        create(client, user_headers, url=f"https://example.org/{i}")
    register_and_verify(client, "other@example.com")
    other = auth_headers(client, "other@example.com")
    create(client, other, url="https://example.org/other")

    page = client.get("/api/v1/links?limit=2", headers=user_headers).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert page["items"][0]["target_url"].endswith("/2")  # newest first
    rest = client.get("/api/v1/links?limit=2&offset=2", headers=user_headers).json()
    assert len(rest["items"]) == 1
    assert client.get("/api/v1/links?limit=500", headers=user_headers).status_code == 422

    mine = {item["id"] for item in page["items"] + rest["items"]}
    theirs = client.get("/api/v1/links", headers=other).json()["items"]
    assert len(theirs) == 1 and theirs[0]["id"] not in mine


def test_delete_own_link_only(client, user_headers):
    link = create(client, user_headers, alias="to-delete").json()
    assert client.get("/to-delete", follow_redirects=False).status_code == 302  # warms the cache

    register_and_verify(client, "other@example.com")
    other = auth_headers(client, "other@example.com")
    assert client.delete(f"/api/v1/links/{link['id']}", headers=other).status_code == 404
    assert client.get(f"/api/v1/links/{link['id']}", headers=other).status_code == 404

    assert client.delete(f"/api/v1/links/{link['id']}", headers=user_headers).status_code == 204
    assert client.get("/to-delete", follow_redirects=False).status_code == 404  # cache was invalidated


def test_links_require_auth(client):
    assert client.post("/api/v1/links", json={"url": "https://example.org"}).status_code == 401
    assert client.get("/api/v1/links").status_code == 401
