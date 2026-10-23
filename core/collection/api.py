from contextlib import contextmanager
import json
import hmac
import secrets
import os
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import portalocker
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from markdownify import markdownify

from .adapters import WechatAdapter, SourceError, adapter, validate_url
from .config import ROOT, Settings
from .db import Database, as_dict
from .models import Admin, Article, Asset, Attempt, Event, Feed, Job, Source, Version
from .security import Vault, check_password, hash_password
from .services import BLOCKS, TERMINAL, block_source, enqueue, initialize, resume


class PasswordInput(BaseModel):
    password: str = Field(min_length=12, max_length=128)


class FeedInput(BaseModel):
    source_id: str = Field(default="primary", pattern="^(primary|weread)$")
    collection_mode: str = Field(default="list", pattern="^(list|latest)$")
    external_id: str = Field(min_length=1, max_length=200, pattern=r"^[\w=+/-]+$")
    name: str = Field(min_length=1, max_length=200)
    interval_minutes: int = Field(default=360, ge=60, le=10080)
    archive_images: bool = False


class FeedSettings(BaseModel):
    collection_mode: str | None = Field(default=None, pattern="^(list|latest)$")
    enabled: bool = True
    interval_minutes: int = Field(default=360, ge=60, le=10080)
    archive_images: bool = False


class SyncInput(BaseModel):
    mode: str = Field(default="incremental", pattern="^(incremental|history)$")
    pages: int = Field(default=2, ge=1, le=10)
    days: int = Field(default=30, ge=1, le=3650)


class ImportInput(BaseModel):
    feed_id: str
    url: str = Field(max_length=4096)
    title: str = Field(default="导入文章", max_length=500)


class SearchInput(BaseModel):
    source_id: str = Field(default="primary", pattern="^(primary|weread)$")
    keyword: str = Field(min_length=1, max_length=100)


def create_app(settings=None):
    settings = settings or Settings()
    db = Database(settings)
    initialize(db)
    vault = Vault(settings)
    app = FastAPI(title="公众号采集工作台", version="0.1.0")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv("COLLECTOR_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(","))
    app.state.db, app.state.settings = db, settings
    login_attempts = {}

    @app.middleware("http")
    async def headers(request, call_next):
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不接受跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def auth(request: Request):
        with db.session() as session:
            admin = session.get(Admin, 1)
            if not admin or not vault.verify_session(request.cookies.get("collector_session"), admin.password_hash):
                raise HTTPException(401, "请先登录")

    def get_row(session, model, identifier):
        value = session.get(model, identifier)
        if not value:
            raise HTTPException(404, "记录不存在")
        return value

    @contextmanager
    def source_access(auth_flow=False, source_id="primary"):
        try:
            with portalocker.Lock(str(settings.data_dir / "source.lock"), timeout=0):
                with db.session(immediate=True) as session:
                    source = get_row(session, Source, source_id)
                    if source.paused_until and source.paused_until > time.time():
                        raise HTTPException(409, "来源处于冷却期")
                    if not auth_flow and source.state != "active":
                        raise HTTPException(409, "来源未授权或已暂停")
                    delay = 3 if auth_flow else settings.request_interval
                    if source.last_request_at + delay > time.time():
                        raise HTTPException(429, "请稍后再试")
                    if session.scalar(select(Job.id).where(Job.status == "running", Job.lease_until > time.time())):
                        raise HTTPException(409, "当前采集任务尚未结束")
                    source.last_request_at = time.time()
                yield
        except portalocker.exceptions.LockException:
            raise HTTPException(409, "当前正在采集，请稍后再试") from None
        except SourceError as exc:
            if exc.code in BLOCKS:
                with db.session(immediate=True) as session:
                    block_source(session, session.get(Source, source_id), exc.code, str(exc), retry_after=exc.retry_after)
            raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from None

    @app.get("/api/setup")
    def setup_status():
        with db.session() as session:
            return {"required": session.get(Admin, 1) is None, "mock": settings.mock}

    @app.post("/api/setup")
    def setup(data: PasswordInput, response: Response):
        hashed = hash_password(data.password)
        with db.session(immediate=True) as session:
            if session.get(Admin, 1):
                raise HTTPException(409, "管理员已初始化")
            session.add(Admin(id=1, password_hash=hashed))
        response.set_cookie("collector_session", vault.session_token(hashed), httponly=True, samesite="strict", secure=settings.secure_cookie, max_age=43200)
        return {"ok": True}

    @app.post("/api/login")
    def login(data: PasswordInput, request: Request, response: Response):
        host = request.client.host
        attempts = [t for t in login_attempts.get(host, []) if t > time.time() - 300]
        if len(attempts) >= 5:
            raise HTTPException(429, "尝试过多，请五分钟后再试")
        with db.session() as session:
            admin = session.get(Admin, 1)
            if not admin or not check_password(data.password, admin.password_hash):
                login_attempts[host] = attempts + [time.time()]
                raise HTTPException(401, "密码不正确")
        login_attempts.pop(host, None)
        response.set_cookie("collector_session", vault.session_token(admin.password_hash), httponly=True, samesite="strict", secure=settings.secure_cookie, max_age=43200)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie("collector_session")
        return {"ok": True}

    prefix = "/api/v1/collection"
    protected = [Depends(auth)]

    @app.get(prefix + "/health/live")
    def live():
        return {"status": "ok"}

    @app.get(prefix + "/health/ready")
    def ready():
        try:
            with db.session() as session:
                session.execute(select(Source.id).limit(1))
            probe = settings.data_dir / ".readiness"
            probe.write_text("ok")
            probe.unlink(missing_ok=True)
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok", "mock": settings.mock}

    @app.get(prefix + "/overview", dependencies=protected)
    def overview():
        with db.session() as session:
            count = lambda model, *conditions: session.scalar(select(func.count()).select_from(model).where(*conditions))
            return {"feeds": count(Feed), "articles": count(Article), "saved": count(Article, Article.content_status == "saved"), "pending": count(Job, Job.status.in_(["queued", "running", "retry_wait"])), "events": count(Event, Event.resolved_at.is_(None)), "mock": settings.mock}

    @app.get(prefix + "/sources", dependencies=protected)
    def sources():
        with db.session() as session:
            return [as_dict(row) for row in session.scalars(select(Source))]

    @app.post(prefix + "/sources/{source_id}/pause", dependencies=protected)
    def pause(source_id: str):
        with db.session(immediate=True) as session:
            source = get_row(session, Source, source_id)
            # Pausing must never clear authentication or cooldown requirements.
            if source.state == "active":
                block_source(session, source, "MANUAL_PAUSE", "已手动暂停")
        return {"ok": True}

    @app.post(prefix + "/sources/{source_id}/resume", dependencies=protected)
    def restart(source_id: str):
        with db.session(immediate=True) as session:
            source = get_row(session, Source, source_id)
            try:
                resume(session, source, authenticated=settings.mock)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @app.post(prefix + "/auth/qr", dependencies=protected)
    def qr_start():
        if settings.mock:
            raise HTTPException(409, "模拟模式不需要微信授权")
        with source_access(auth_flow=True):
            data = WechatAdapter(settings).qr_start()
            with db.session(immediate=True) as session:
                block_source(session, session.get(Source, "primary"), "AUTH_EXPIRED", "等待扫码授权")
            return data

    @app.post(prefix + "/auth/qr/check", dependencies=protected)
    def qr_check():
        if settings.mock:
            raise HTTPException(409, "模拟模式不需要微信授权")
        with source_access(auth_flow=True):
            data = WechatAdapter(settings).qr_check()
            if data["status"] == "success":
                with db.session(immediate=True) as session:
                    source = session.get(Source, "primary")
                    source.last_auth_at = time.time()
                    resume(session, source, authenticated=True)
            return data

    @app.post(prefix + "/sources/weread/auth/qr", dependencies=protected)
    def weread_qr_start():
        if settings.mock:
            raise HTTPException(409, "模拟模式不需要微信读书授权")
        with source_access(auth_flow=True, source_id="weread"):
            from .weread import WereadAdapter
            data = WereadAdapter(settings).qr_start()
            with db.session(immediate=True) as session:
                block_source(session, session.get(Source, "weread"), "AUTH_EXPIRED", "等待微信读书扫码授权")
            return data

    @app.post(prefix + "/sources/weread/auth/qr/check", dependencies=protected)
    def weread_qr_check():
        if settings.mock:
            raise HTTPException(409, "模拟模式不需要微信读书授权")
        with source_access(auth_flow=True, source_id="weread"):
            from .weread import WereadAdapter
            data = WereadAdapter(settings).qr_check()
            if data["status"] == "success":
                with db.session(immediate=True) as session:
                    source = session.get(Source, "weread")
                    source.last_auth_at = time.time()
                    resume(session, source, authenticated=True)
            return data

    @app.post(prefix + "/search", dependencies=protected)
    def search(data: SearchInput):
        with source_access(source_id=data.source_id):
            return adapter(settings, data.source_id).search(data.keyword)

    @app.get(prefix + "/feeds", dependencies=protected)
    def feeds():
        with db.session() as session:
            return [as_dict(row) for row in session.scalars(select(Feed).order_by(Feed.name))]

    @app.post(prefix + "/feeds", status_code=201, dependencies=protected)
    def create_feed(data: FeedInput):
        if data.source_id != "weread" and data.collection_mode != "list":
            raise HTTPException(422, "仅微信读书支持最新一篇模式")
        if data.source_id == "weread":
            from .weread import BOOK_ID
            if not BOOK_ID.fullmatch(data.external_id):
                raise HTTPException(422, "微信读书公众号标识应为 MP_WXS_数字")
        with db.session(immediate=True) as session:
            old = session.scalar(select(Feed).where(Feed.external_id == data.external_id, Feed.source_id == data.source_id))
            if old:
                return as_dict(old)
            feed = Feed(**data.model_dump())
            session.add(feed)
            session.flush()
            return as_dict(feed)

    @app.get(prefix + "/feeds/{feed_id}/settings", dependencies=protected)
    def feed_settings(feed_id: str):
        with db.session() as session:
            return as_dict(get_row(session, Feed, feed_id))

    @app.put(prefix + "/feeds/{feed_id}/settings", dependencies=protected)
    def update_feed(feed_id: str, data: FeedSettings):
        with db.session(immediate=True) as session:
            feed = get_row(session, Feed, feed_id)
            if data.collection_mode == "latest" and feed.source_id != "weread":
                raise HTTPException(422, "仅微信读书支持最新一篇模式")
            mode_changed = data.collection_mode is not None and data.collection_mode != feed.collection_mode
            if mode_changed:
                if session.scalar(select(Job.id).where(Job.feed_id == feed.id, Job.status == "running")):
                    raise HTTPException(409, "请等待当前采集结束后切换模式")
                feed.history_cursor, feed.coverage = 0, "unverified"
                for job in session.scalars(select(Job).where(Job.feed_id == feed.id, Job.kind == "discover", Job.active_key.is_not(None))):
                    job.status, job.active_key, job.ended_at = "cancelled", None, time.time()
            for key, value in data.model_dump(exclude_none=True).items():
                setattr(feed, key, value)
            feed.next_run_at = time.time() + data.interval_minutes * 60
            if not feed.enabled:
                for job in session.scalars(select(Job).where(Job.feed_id == feed.id, Job.active_key.is_not(None))):
                    if job.status == "running":
                        job.cancel_requested = True
                    else:
                        job.status, job.active_key = "cancelled", None
            return as_dict(feed)

    @app.post(prefix + "/feeds/{feed_id}/sync", status_code=202, dependencies=protected)
    def sync(feed_id: str, data: SyncInput, request: Request):
        with db.session(immediate=True) as session:
            feed = get_row(session, Feed, feed_id)
            if not feed.enabled:
                raise HTTPException(409, "请先启用公众号")
            history = data.mode == "history"
            if history and feed.collection_mode == "latest":
                raise HTTPException(409, "仅最新一篇模式无法补采历史，也不能保证两次检查之间没有遗漏")
            key = request.headers.get("idempotency-key")
            payload = {"offset": (feed.history_cursor if feed.source_id == "weread" else max(0, feed.history_cursor - 5)) if history else 0, "remaining": data.pages, "history": history}
            if history:
                payload["since"] = time.time() - data.days * 86400
            job = enqueue(session, kind="discover", feed_id=feed.id, payload=payload, key=f"sync:{feed.id}:{key}" if key else None, priority=80 if history else 40)
            return as_dict(job)

    @app.get(prefix + "/jobs", dependencies=protected)
    def jobs(status: str | None = None, offset: int = 0, limit: int = 50):
        with db.session() as session:
            query = select(Job)
            if status:
                query = query.where(Job.status == status)
            return [as_dict(row) for row in session.scalars(query.order_by(Job.created_at.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 100)))]

    @app.get(prefix + "/jobs/{job_id}", dependencies=protected)
    def job_detail(job_id: str):
        with db.session() as session:
            job = get_row(session, Job, job_id)
            return {**as_dict(job), "attempts_log": [as_dict(row) for row in session.scalars(select(Attempt).where(Attempt.job_id == job_id))]}

    @app.post(prefix + "/jobs/{job_id}/retry", dependencies=protected)
    def retry(job_id: str):
        with db.session(immediate=True) as session:
            job = get_row(session, Job, job_id)
            source = session.get(Source, job.source_id)
            if source.state != "active":
                raise HTTPException(409, "请先处理来源暂停原因")
            if job.status not in TERMINAL:
                raise HTTPException(409, "任务仍处于活动状态")
            feed = session.get(Feed, job.feed_id) if job.feed_id else None
            if feed and feed.collection_mode == "latest" and job.payload.get("history"):
                raise HTTPException(409, "仅最新一篇模式无法重试历史补采任务")
            new = enqueue(session, kind=job.kind, feed_id=job.feed_id, article_id=job.article_id, payload=job.payload, priority=job.priority)
            return as_dict(new)

    @app.post(prefix + "/jobs/{job_id}/cancel", dependencies=protected)
    def cancel(job_id: str):
        with db.session(immediate=True) as session:
            job = get_row(session, Job, job_id)
            if job.status == "running":
                job.cancel_requested = True
            elif job.status not in TERMINAL:
                job.status, job.active_key, job.ended_at = "cancelled", None, time.time()
            return as_dict(job)

    @app.get(prefix + "/articles", dependencies=protected)
    def articles(q: str = "", feed_id: str | None = None, since: float | None = None, until: float | None = None, offset: int = 0, limit: int = 30):
        with db.session() as session:
            query = select(Article)
            if q:
                query = query.where(or_(Article.title.contains(q, autoescape=True), Article.plain_text.contains(q, autoescape=True)))
            if feed_id:
                query = query.where(Article.feed_id == feed_id)
            if since is not None:
                query = query.where(Article.published_at >= since)
            if until is not None:
                query = query.where(Article.published_at <= until)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(query.order_by(Article.published_at.desc(), Article.discovered_at.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 100)))
            return {"total": total, "items": [{k: v for k, v in as_dict(row).items() if k not in ("content_html", "plain_text")} for row in rows]}

    @app.get(prefix + "/articles/{article_id}", dependencies=protected)
    def article_detail(article_id: str):
        with db.session() as session:
            article = get_row(session, Article, article_id)
            data = as_dict(article)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(data["content_html"], "html.parser")
            saved = {a.source_url: a.id for a in session.scalars(select(Asset).where(Asset.article_id == article.id, Asset.status == "saved"))}
            for image in soup.find_all("img"):
                if image.get("src") in saved:
                    image["src"] = f"{prefix}/assets/{saved[image['src']]}"
            data["content_html"] = str(soup)
            data["versions"] = [as_dict(v) for v in session.scalars(select(Version).where(Version.article_id == article.id).order_by(Version.captured_at.desc()))]
            return data

    @app.get(prefix + "/assets/{asset_id}", dependencies=protected)
    def asset_file(asset_id: int):
        with db.session() as session:
            asset = get_row(session, Asset, asset_id)
            if asset.status != "saved" or not asset.local_path:
                raise HTTPException(404, "图片未归档")
            path = (settings.data_dir / asset.local_path).resolve()
            if not path.is_relative_to(settings.data_dir / "assets"):
                raise HTTPException(404)
            return FileResponse(path)

    @app.post(prefix + "/articles/{article_id}/refresh", status_code=202, dependencies=protected)
    def refresh(article_id: str):
        with db.session(immediate=True) as session:
            article = get_row(session, Article, article_id)
            return as_dict(enqueue(session, kind="fetch", feed_id=article.feed_id, article_id=article.id, priority=30))

    @app.post(prefix + "/imports", status_code=202, dependencies=protected)
    def import_url(data: ImportInput):
        try:
            validate_url(data.url, resolve=False)
        except (SourceError, ValueError):
            raise HTTPException(422, "请输入微信 HTTPS 文章链接") from None
        with db.session(immediate=True) as session:
            feed = get_row(session, Feed, data.feed_id)
            if feed.source_id == "weread":
                raise HTTPException(422, "微信读书订阅请从书架获取文章列表，单篇微信链接请使用公众平台订阅")
            return as_dict(enqueue(session, kind="import", feed_id=feed.id, payload={"url": data.url, "title": data.title}, priority=30))

    @app.get(prefix + "/events", dependencies=protected)
    def events():
        with db.session() as session:
            return [as_dict(row) for row in session.scalars(select(Event).order_by(Event.created_at.desc()).limit(100))]

    @app.get(prefix + "/exports/{feed_id}", dependencies=protected)
    def export(feed_id: str, format: str = "json"):
        if format not in {"json", "markdown", "rss"}:
            raise HTTPException(422, "支持 json、markdown、rss")
        with db.session() as session:
            feed = get_row(session, Feed, feed_id)
            rows = list(session.scalars(select(Article).where(Article.feed_id == feed.id).order_by(Article.published_at.desc())))
            if format == "json":
                body, mime, extension = json.dumps([as_dict(a) for a in rows], ensure_ascii=False, indent=2), "application/json", "json"
            elif format == "markdown":
                body = "\n\n---\n\n".join(f"# {a.title}\n\n原文：{a.url}\n\n{markdownify(a.content_html)}" for a in rows)
                mime, extension = "text/markdown", "md"
            else:
                root = ET.Element("rss", version="2.0")
                channel = ET.SubElement(root, "channel")
                ET.SubElement(channel, "title").text = feed.name
                ET.SubElement(channel, "link").text = "https://mp.weixin.qq.com/"
                ET.SubElement(channel, "description").text = "已归档文章；覆盖范围以来源实际可访问内容为准"
                from email.utils import formatdate
                for article in rows:
                    item = ET.SubElement(channel, "item")
                    ET.SubElement(item, "title").text = article.title
                    ET.SubElement(item, "link").text = article.url
                    ET.SubElement(item, "guid", isPermaLink="false").text = article.id
                    ET.SubElement(item, "description").text = article.content_html
                    if article.published_at:
                        ET.SubElement(item, "pubDate").text = formatdate(article.published_at, usegmt=True)
                body, mime, extension = ET.tostring(root, encoding="unicode"), "application/rss+xml", "xml"
            return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="articles.{extension}"'})

    @app.post(prefix + "/feeds/{feed_id}/rss-link", dependencies=protected)
    def rss_link(feed_id: str, request: Request, rotate: bool = False):
        with db.session() as session:
            get_row(session, Feed, feed_id)
        name = "rss-" + feed_id
        saved = vault.load(name)
        if rotate or not saved:
            saved = {"token": secrets.token_urlsafe(32)}
            vault.save(name, saved)
        return {"url": str(request.base_url).rstrip("/") + f"/rss/{feed_id}?token={saved['token']}"}

    @app.get("/rss/{feed_id}")
    def rss_subscription(feed_id: str, token: str = ""):
        # Validate ID before using it in any filesystem path.
        import uuid
        try:
            uuid.UUID(feed_id)
        except ValueError:
            raise HTTPException(404) from None
        saved = vault.load("rss-" + feed_id)
        if not saved or not hmac.compare_digest(saved["token"], token):
            raise HTTPException(403, "RSS 链接无效或已撤销")
        response = export(feed_id, format="rss")
        del response.headers["content-disposition"]
        return response

    build = ROOT / "static" / "collector"
    if (build / "assets").exists():
        app.mount("/assets", StaticFiles(directory=build / "assets"), name="collector-assets")

    @app.get("/")
    def index():
        if not (build / "index.html").exists():
            return JSONResponse({"message": "请先构建 collection-ui，或访问 /docs 查看 API。"})
        return FileResponse(build / "index.html")

    return app


def factory():
    return create_app()
