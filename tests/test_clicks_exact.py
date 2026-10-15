"""Click counting must be exact: no click lost to a crash, none counted twice."""

import asyncio
import time
from datetime import datetime, timezone

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text

from app.services import clicks


def make_link(client, headers, alias=None):
    r = client.post("/api/v1/links", headers=headers, json={"url": "https://example.org/t", "alias": alias})
    assert r.status_code == 201, r.text
    return r.json()


def count(client, headers, link):
    return client.get(f"/api/v1/links/{link['id']}", headers=headers).json()["click_count"]


def hit(client, link, n):
    for _ in range(n):
        assert client.get(f"/{link['code']}", follow_redirects=False).status_code == 302


def stream_len(client):
    return client.portal.call(client.app_ref.state.redis.xlen, clicks.STREAM)


def test_crash_after_commit_before_trim_never_double_counts(client, user_headers, flush, monkeypatch):
    link = make_link(client, user_headers)
    hit(client, link, 5)
    redis = client.app_ref.state.redis
    real_xtrim = redis.xtrim

    async def failing_xtrim(*a, **k):
        raise RedisConnectionError("died right after the DB commit")

    monkeypatch.setattr(redis, "xtrim", failing_xtrim)
    assert flush() == 5
    assert count(client, user_headers, link) == 5
    assert stream_len(client) == 5  # entries are still in Redis because the trim "crashed"

    monkeypatch.setattr(redis, "xtrim", real_xtrim)
    assert flush() == 0  # the watermark already covers them: nothing is counted again
    assert count(client, user_headers, link) == 5
    assert stream_len(client) == 0  # and the leftover entries were cleaned up


def test_failure_before_commit_loses_nothing_and_retries_exactly(client, user_headers, flush):
    link = make_link(client, user_headers)
    hit(client, link, 4)
    app = client.app_ref

    class BrokenSessionmaker:  # PostgreSQL is "down" for this attempt
        def __call__(self):
            raise ConnectionError("postgres is down")

    try:
        client.portal.call(clicks.flush_clicks, app.state.redis, BrokenSessionmaker())
        raise AssertionError("flush should have failed")
    except ConnectionError:
        pass
    assert count(client, user_headers, link) == 0 and stream_len(client) == 4  # nothing counted, nothing lost

    assert flush() == 4
    assert count(client, user_headers, link) == 4  # counted once, on the retry
    assert flush() == 0


def test_large_backlog_is_flushed_in_batches(client, user_headers):
    a, b = make_link(client, user_headers), make_link(client, user_headers)
    hit(client, a, 7)
    hit(client, b, 3)
    app = client.app_ref
    total = client.portal.call(lambda: clicks.flush_clicks(app.state.redis, app.state.sessionmaker, batch=3))
    assert total == 10
    assert count(client, user_headers, a) == 7 and count(client, user_headers, b) == 3
    assert stream_len(client) == 0


def test_concurrent_flushers_count_each_click_once(client, user_headers):
    link = make_link(client, user_headers)
    hit(client, link, 12)
    app = client.app_ref

    async def race():
        return await asyncio.gather(*(clicks.flush_clicks(app.state.redis, app.state.sessionmaker, batch=2) for _ in range(4)))

    results = client.portal.call(race)
    assert sum(results) == 12
    assert count(client, user_headers, link) == 12


def test_clicks_during_flush_are_kept(client, user_headers, flush):
    link = make_link(client, user_headers)
    hit(client, link, 2)
    assert flush() == 2
    hit(client, link, 3)
    assert flush() == 3
    assert count(client, user_headers, link) == 5


def test_last_clicked_at_is_the_click_time_not_the_flush_time(client, user_headers, flush):
    link = make_link(client, user_headers)
    before = datetime.now(timezone.utc)
    hit(client, link, 1)
    time.sleep(1.5)
    flush()
    got = client.get(f"/api/v1/links/{link['id']}", headers=user_headers).json()["last_clicked_at"]
    clicked_at = datetime.fromisoformat(got.replace("Z", "+00:00"))
    assert 0 <= (clicked_at - before).total_seconds() < 1.0


def test_clicks_for_deleted_links_are_discarded_cleanly(client, user_headers, flush):
    gone, kept = make_link(client, user_headers), make_link(client, user_headers)
    hit(client, gone, 2)
    hit(client, kept, 1)
    client.delete(f"/api/v1/links/{gone['id']}", headers=user_headers)
    assert flush() == 3  # the stream is drained; the update for the deleted link simply matches no row
    assert count(client, user_headers, kept) == 1


def test_head_requests_are_not_clicks(client, user_headers, flush):
    link = make_link(client, user_headers)
    client.head(f"/{link['code']}", follow_redirects=False)
    assert flush() == 0


def test_legacy_hash_buffers_are_drained_on_upgrade(client, user_headers, flush):
    link = make_link(client, user_headers)
    redis = client.app_ref.state.redis
    client.portal.call(redis.hset, "clicks:pending", str(link["id"]), 6)  # written by the previous version
    client.portal.call(redis.hset, "clicks:processing", str(link["id"]), 4)
    flush()
    assert count(client, user_headers, link) == 10
    assert client.portal.call(redis.exists, "clicks:pending", "clicks:processing") == 0


def test_database_failure_during_fallback_is_counted_as_lost(client, user_headers, monkeypatch):
    link = make_link(client, user_headers)
    app = client.app_ref

    async def boom(*a, **k):
        raise RedisConnectionError("down")

    monkeypatch.setattr(app.state.redis, "xadd", boom)

    class DeadSessionmaker:
        def __call__(self):
            raise ConnectionError("postgres down too")

    before = text("select 1")  # noqa: F841
    from app.metrics import CLICKS_LOST

    start = CLICKS_LOST._value.get()
    client.portal.call(clicks.record_click, app.state.redis, DeadSessionmaker(), link["id"])
    assert CLICKS_LOST._value.get() == start + 1
