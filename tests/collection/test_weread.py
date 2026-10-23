import json
import time
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import select

from core.collection.api import create_app
from core.collection.config import Settings
from core.collection.models import Article, Feed, Job, Source
from core.collection.security import Vault
from core.collection.services import block_source, enqueue, schedule
from core.collection.weread import WereadAdapter, article_url, checked_json
from core.collection.adapters import SourceError
from core.collection.worker import Worker

PREFIX = "/api/v1/collection"
BOOK = "MP_WXS_123456"


def response(data, status=200):
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(data).encode()
    value._content_consumed = True
    value.headers["content-type"] = "application/json"
    return value


@pytest.fixture
def adapter(tmp_path):
    settings = Settings(data_dir=tmp_path)
    Vault(settings).save("weread", {"cookies": {"wr_skey": "private-token", "wr_vid": "123"}})
    return WereadAdapter(settings)


def page():
    return {"reviews": [{"createTime": 1700000000, "subReviews": [
        {"review": {"reviewId": BOOK + "_abc~xyz", "mpInfo": {"title": "第一篇", "originalId": "abc~xyz"}}},
        {"review": {"reviewId": BOOK + "_def", "mpInfo": {"title": "第二篇"}}},
    ]}]}


def test_groups_pagination_and_shortlinks(adapter, monkeypatch):
    request = Mock(return_value=response(page()))
    monkeypatch.setattr(adapter, "request", request)
    result = adapter.discover({"external_id": BOOK, "name": "测试"}, 5)
    assert len(result["items"]) == 2
    assert result["next_cursor"] == 6 and not result["exhausted"]
    assert result["items"][0]["url"] == "https://mp.weixin.qq.com/s/abc_xyz"
    assert result["items"][1]["url"] == "https://mp.weixin.qq.com/s/def"
    request.assert_called_once_with("/web/mp/articles", {"bookId": BOOK, "offset": 5})
    request.return_value = response({"reviews": []})
    assert adapter.discover({"external_id": BOOK})["exhausted"]


@pytest.mark.parametrize("data", [{}, {"reviews": {}}, {"reviews": [{"unexpected": []}]}])
def test_invalid_lists_do_not_report_success(adapter, monkeypatch, data):
    monkeypatch.setattr(adapter, "request", lambda *a, **k: response(data))
    with pytest.raises(SourceError):
        adapter.discover({"external_id": BOOK})


@pytest.mark.parametrize("code,kind", [(-2012, "AUTH_EXPIRED"), (-2041, "SOURCE_CHANGED"), (-9999, "SOURCE_CHANGED")])
def test_diagnostic_codes_never_include_raw_error_secrets(code, kind):
    with pytest.raises(SourceError) as exc:
        checked_json(response({"errCode": code, "errMsg": "secret-cookie-token"}), "/web/mp/articles")
    assert exc.value.code == kind
    assert str(code) in str(exc.value) and "secret-cookie-token" not in str(exc.value)


def test_domain_scoped_cookies_and_no_redirect(adapter, monkeypatch):
    for url in ("https://mp.weixin.qq.com/s/test", "https://mmbiz.qpic.cn/test"):
        prepared = adapter.session.prepare_request(requests.Request("GET", url))
        assert "Cookie" not in prepared.headers
    limited = response({}, 429)
    limited.headers["Retry-After"] = "30000"
    get = Mock(return_value=limited)
    monkeypatch.setattr(adapter.session, "get", get)
    with pytest.raises(SourceError) as exc:
        adapter.request("/web/mp/articles")
    assert exc.value.code == "RATE_LIMITED" and exc.value.retry_after == 30000
    assert get.call_count == 1 and get.call_args.kwargs["allow_redirects"] is False


def test_shelf_only_returns_matching_public_accounts(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "request", lambda *a, **k: response({"books": [
        {"bookId": BOOK, "title": "猫笔刀"}, {"bookId": "12345", "title": "猫笔刀的书"},
        {"bookId": "MP_WXS_999", "title": "其他公众号"},
    ]}))
    result = adapter.search("猫笔刀")
    assert [x["external_id"] for x in result["accounts"]] == [BOOK]


def test_qr_verifies_before_storing_credentials(adapter, monkeypatch):
    adapter.vault.save("weread_pending", {"uid": "private-uid", "expires": time.time() + 60, "cookies": {}})
    monkeypatch.setattr(adapter, "request", lambda path, *a, **k: response(
        {"succeed": True, "vid": "123", "accessToken": "new-secret"} if "LoginInfo" in path else {"errCode": -2012}))
    with pytest.raises(SourceError):
        adapter.qr_check()
    assert adapter.vault.load("weread")["cookies"]["wr_skey"] == "private-token"
    monkeypatch.setattr(adapter, "shelf", lambda: [])
    assert adapter.qr_check() == {"status": "success"}
    assert adapter.vault.load("weread")["cookies"]["wr_skey"] == "new-secret"
    assert not adapter.vault.load("weread_pending")


def test_qr_timeout_is_pending_and_expiration_stops_request(adapter, monkeypatch):
    adapter.vault.save("weread_pending", {"uid": "private-uid", "expires": time.time() + 60})
    request = Mock(return_value=None)
    monkeypatch.setattr(adapter, "request", request)
    assert adapter.qr_check() == {"status": "waiting"}
    adapter.vault.save("weread_pending", {"uid": "private-uid", "expires": time.time() - 1})
    assert adapter.qr_check() == {"status": "expired"}
    assert request.call_count == 1


def test_qr_svg_generated_locally(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "request", lambda *a, **k: response({"uid": "local-qr-uid"}))
    result = adapter.qr_start()
    assert result["image"].startswith("data:image/svg+xml;base64,")
    assert result["expires_in"] == 240
    assert "local-qr-uid" not in str(result)


def test_body_uses_weread_endpoint_and_sanitizes(adapter, monkeypatch):
    value = response({})
    value._content = b'<div id="js_content"><script>evil()</script><p>Hello</p></div>'
    value.headers["content-type"] = "text/html"
    request = Mock(return_value=value)
    monkeypatch.setattr(adapter, "request", request)
    result = adapter.fetch({"source_article_id": BOOK + "_abc"})
    assert result["text"] == "Hello" and "script" not in result["html"]
    request.assert_called_once_with("/web/mp/content", {"reviewId": BOOK + "_abc"})
    with pytest.raises(SourceError):
        adapter.fetch({"url": "https://mp.weixin.qq.com/s/abc"})


def test_blocked_primary_does_not_stop_weread_and_jobs_use_feed_source(tmp_path):
    settings = Settings(data_dir=tmp_path, mock=True, request_interval=0)
    app = create_app(settings)
    db = app.state.db
    client = TestClient(app)
    assert client.post(PREFIX + "/search", json={"source_id": "weread", "keyword": "test"}).status_code == 401
    client.post("/api/setup", json={"password": "test-weread-password"})
    primary = client.post(PREFIX + "/feeds", json={"name": "原渠道", "external_id": "old"}).json()
    wr = client.post(PREFIX + "/feeds", json={"source_id": "weread", "name": "新渠道", "external_id": BOOK}).json()
    with db.session(immediate=True) as session:
        block_source(session, session.get(Source, "primary"), "RATE_LIMITED", "原渠道限流")
    schedule(db)
    worker = Worker(settings)
    for _ in range(15):
        if not worker.once(subprocess=False):
            break
    with db.session() as session:
        assert session.get(Source, "primary").state == "blocked_rate"
        assert session.get(Source, "weread").state == "active"
        jobs = list(session.scalars(select(Job)))
        assert jobs and all(j.source_id == "weread" and j.status == "succeeded" for j in jobs)
        assert len(list(session.scalars(select(Article).where(Article.feed_id == wr["id"])))) == 3
    assert client.post(PREFIX + "/imports", json={"feed_id": wr["id"], "url": "https://mp.weixin.qq.com/s/abc"}).status_code == 422
    assert client.post(PREFIX + "/feeds", json={"source_id": "weread", "name": "bad", "external_id": "fakeid"}).status_code == 422
    assert client.post(PREFIX + "/sources/primary/resume").status_code == 409


def test_real_adapter_pipeline_and_repeated_sync_deduplicates(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    db = app.state.db
    calls = []

    def transport(self, path, params=None, auth=True):
        calls.append(path)
        if path == "/web/mp/articles":
            data = page()
            data["hasMore"] = False
            return response(data)
        assert path == "/web/mp/content"
        result = response({})
        result.headers["content-type"] = "text/html"
        result._content = '<div id="js_content"><p>正文归档验证</p></div>'.encode()
        return result

    monkeypatch.setattr(WereadAdapter, "request", transport)
    with db.session(immediate=True) as session:
        session.get(Source, "weread").state = "active"
        feed = Feed(name="读书来源", external_id=BOOK, source_id="weread")
        session.add(feed)
        session.flush()
        feed_id = feed.id
        enqueue(session, kind="discover", feed_id=feed_id)
    worker = Worker(settings)
    for _ in range(3):
        with db.session(immediate=True) as session:
            session.get(Source, "weread").last_request_at = 0
        assert worker.once(subprocess=False)
    with db.session(immediate=True) as session:
        rows = list(session.scalars(select(Article)))
        assert len(rows) == 2 and all(a.content_status == "saved" for a in rows)
        assert all(a.plain_text == "正文归档验证" for a in rows)
        assert session.get(Source, "primary").state == "unconfigured"
        session.get(Source, "weread").last_request_at = 0
        enqueue(session, kind="discover", feed_id=feed_id)
    assert worker.once(subprocess=False)
    with db.session() as session:
        assert len(list(session.scalars(select(Article)))) == 2
        assert session.get(Feed, feed_id).coverage == "weread_list"
    assert calls == ["/web/mp/articles", "/web/mp/content", "/web/mp/content", "/web/mp/articles"]


def test_weread_qr_api_keeps_public_platform_cooldown(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    db = app.state.db
    client.post("/api/setup", json={"password": "test-weread-password"})
    with db.session(immediate=True) as session:
        block_source(session, session.get(Source, "primary"), "RATE_LIMITED", "原渠道限流")
        cooldown = session.get(Source, "primary").paused_until
    monkeypatch.setattr(WereadAdapter, "qr_start", lambda self: {"image": "test-qr", "expires_in": 240})
    assert client.post(PREFIX + "/sources/weread/auth/qr").status_code == 200
    with db.session(immediate=True) as session:
        session.get(Source, "weread").last_request_at = 0
    monkeypatch.setattr(WereadAdapter, "qr_check", lambda self: {"status": "success"})
    assert client.post(PREFIX + "/sources/weread/auth/qr/check").json()["status"] == "success"
    with db.session() as session:
        assert session.get(Source, "weread").state == "active"
        assert session.get(Source, "primary").paused_until == cooldown
        assert session.get(Source, "primary").state == "blocked_rate"


def test_latest_mode_is_explicit_and_does_not_invent_time(adapter, monkeypatch):
    request = Mock(return_value=response({"name": "猫笔刀", "title": "最新文章", "reviewId": BOOK + "_latest~id"}))
    monkeypatch.setattr(adapter, "request", request)
    result = adapter.discover({"external_id": BOOK, "collection_mode": "latest"})
    assert result["coverage"] == "latest_only" and result["exhausted"]
    assert len(result["items"]) == 1 and result["items"][0]["published_at"] is None
    assert result["items"][0]["url"].endswith("latest_id")
    request.assert_called_once_with("/api/mp/cover", {"bookId": BOOK})
    request.reset_mock()
    request.return_value = response({"errCode": -2041})
    with pytest.raises(SourceError):
        adapter.discover({"external_id": BOOK, "collection_mode": "list"})
    request.assert_called_once_with("/web/mp/articles", {"bookId": BOOK, "offset": 0})


def test_mode_change_cancels_old_discovery_and_rejects_history(tmp_path):
    settings = Settings(data_dir=tmp_path, mock=True, request_interval=0)
    app = create_app(settings)
    db = app.state.db
    client = TestClient(app)
    client.post("/api/setup", json={"password": "test-weread-password"})
    item = client.post(PREFIX + "/feeds", json={"source_id": "weread", "name": "测试", "external_id": BOOK}).json()
    job = client.post(PREFIX + f"/feeds/{item['id']}/sync", json={"mode": "history"}).json()
    with db.session(immediate=True) as session:
        session.get(Feed, item["id"]).history_cursor = 100
        block_source(session, session.get(Source, "weread"), "SOURCE_CHANGED", "list unavailable")
    result = client.put(PREFIX + f"/feeds/{item['id']}/settings", json={"collection_mode": "latest"})
    assert result.status_code == 200 and result.json()["history_cursor"] == 0
    with db.session() as session:
        old = session.get(Job, job["id"])
        assert old.status == "cancelled" and old.active_key is None
        assert session.get(Source, "weread").state == "blocked_source"
    assert client.post(PREFIX + f"/feeds/{item['id']}/sync", json={"mode": "history"}).status_code == 409
    # Older clients that omit collection_mode must not silently reset the setting.
    assert client.put(PREFIX + f"/feeds/{item['id']}/settings", json={"interval_minutes": 720}).json()["collection_mode"] == "latest"


def test_002_migration_preserves_existing_feeds(tmp_path):
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from core.collection.config import ROOT
    from core.collection.db import Database
    from core.collection.services import initialize

    settings = Settings(data_dir=tmp_path)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations_collector"))
    config.set_main_option("sqlalchemy.url", settings.db_url)
    command.upgrade(config, "001")
    with sqlite3.connect(tmp_path / "collector.db") as conn:
        conn.execute("insert into source_accounts(id,provider,state) values('primary','wechat','blocked_rate')")
        conn.execute("insert into feeds(id,source_id,external_id,name) values('old','primary','old-biz','Existing feed')")
        conn.commit()
    db = Database(settings)
    initialize(db)
    with db.session() as session:
        assert session.get(Feed, "old").collection_mode == "list"
        assert session.get(Feed, "old").name == "Existing feed"
        assert session.get(Source, "primary").state == "blocked_rate"
