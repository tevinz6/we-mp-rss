import base64
import hashlib
import os
import time
import uuid
from pathlib import Path
from sqlalchemy import select
from .models import Article, Asset, Attempt, Event, Feed, Job, Source, Version
from .adapters import canonical_url

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
BLOCKS = {"AUTH_EXPIRED": "blocked_auth", "RATE_LIMITED": "blocked_rate", "CHALLENGE_REQUIRED": "blocked_challenge", "SOURCE_CHANGED": "blocked_source", "PARSE_ERROR": "blocked_source"}


def initialize(db):
    db.migrate()
    with db.session(immediate=True) as session:
        source = session.get(Source, "primary")
        provider = "mock" if db.settings.mock else "wechat"
        if source and source.provider != provider:
            raise RuntimeError("Mock and live sources require separate data directories.")
        if not source:
            session.add(Source(id="primary", provider=provider, state="active" if db.settings.mock else "unconfigured"))
        if not session.get(Source, "weread"):
            session.add(Source(id="weread", provider="mock" if db.settings.mock else "weread", state="active" if db.settings.mock else "unconfigured"))


def enqueue(session, *, kind, feed_id=None, article_id=None, payload=None, key=None, priority=50, run_after=None):
    payload = payload or {}
    resource = article_id or feed_id or "primary"
    if kind == "archive_image":
        resource += ":" + hashlib.sha256(payload["url"].encode()).hexdigest()
    if kind == "import":
        resource += ":" + hashlib.sha256(canonical_url(payload["url"]).encode()).hexdigest()
    active = f"{kind}:{resource}"
    if key:
        old = session.scalar(select(Job).where(Job.idempotency_key == key))
        if old:
            return old
    old = session.scalar(select(Job).where(Job.active_key == active))
    if old:
        return old
    feed = session.get(Feed, feed_id) if feed_id else None
    source = session.get(Source, feed.source_id if feed else "primary")
    state = "queued" if source.state == "active" else "blocked_auth" if source.state == "unconfigured" else "blocked_source"
    job = Job(source_id=source.id, kind=kind, feed_id=feed_id, article_id=article_id, payload=payload, idempotency_key=key or str(uuid.uuid4()), active_key=active, priority=priority, status=state, run_after=run_after or time.time())
    session.add(job)
    session.flush()
    return job


def schedule(db):
    now = time.time()
    with db.session(immediate=True) as session:
        for feed in session.scalars(select(Feed).join(Source, Feed.source_id == Source.id).where(Source.state == "active", Feed.enabled.is_(True), Feed.next_run_at <= now)):
            enqueue(session, kind="discover", feed_id=feed.id, payload={"offset": 0, "remaining": 2}, key=f"schedule:{feed.id}:{int(feed.next_run_at)}", priority=40)
            feed.next_run_at = now + feed.interval_minutes * 60


def event_once(session, source, job_id, code, message):
    previous = session.scalar(select(Event).where(Event.source_id == source.id, Event.code == code, Event.resolved_at.is_(None)))
    if not previous:
        session.add(Event(source_id=source.id, job_id=job_id, code=code, message=message))


def block_source(session, source, code, message, job_id=None, retry_after=0):
    source.state = BLOCKS.get(code, "blocked_source")
    source.reason = message
    source.paused_until = time.time() + max(21600, retry_after) if code == "RATE_LIMITED" else 0
    for pending in session.scalars(select(Job).where(Job.source_id == source.id, Job.status.in_(["queued", "retry_wait"]))):
        pending.status = source.state
    event_once(session, source, job_id, code, message)


def resume(session, source, authenticated=False):
    if source.paused_until and source.paused_until > time.time():
        raise ValueError("冷却尚未结束")
    if source.state in ("unconfigured", "blocked_auth") and not authenticated:
        raise ValueError("请先重新扫码授权")
    source.state, source.reason, source.paused_until = "active", None, 0
    source.network_failures = 0
    for job in session.scalars(select(Job).where(Job.status.like("blocked_%"), Job.source_id == source.id)):
        job.status = "queued"
        job.run_after = time.time()
    for event in session.scalars(select(Event).where(Event.source_id == source.id, Event.resolved_at.is_(None))):
        event.resolved_at = time.time()


def claim(db):
    now = time.time()
    with db.session(immediate=True) as session:
        # Single worker process lock makes it safe to recover its predecessor's expired leases.
        for old in session.scalars(select(Job).where(Job.status == "running", Job.lease_until < now)):
            old.status = "queued"
            old.lease_token = None
            for attempt in session.scalars(select(Attempt).where(Attempt.job_id == old.id, Attempt.ended_at.is_(None))):
                attempt.outcome, attempt.ended_at = "interrupted", now
        session.flush()
        if session.scalar(select(Job.id).where(Job.status == "running").limit(1)):
            return None
        job = session.scalar(select(Job).join(Source, Job.source_id == Source.id).where(Source.state == "active", Source.last_request_at + db.settings.request_interval <= now, Job.status.in_(["queued", "retry_wait"]), Job.run_after <= now).order_by(Job.priority, Job.created_at).limit(1))
        if not job:
            return None
        if job.feed_id:
            feed = session.get(Feed, job.feed_id)
            if not feed or not feed.enabled:
                job.status, job.active_key = "cancelled", None
                return None
        source = session.get(Source, job.source_id)
        job.status, job.lease_token, job.lease_until = "running", str(uuid.uuid4()), now + 180
        job.started_at, job.heartbeat_at = now, now
        job.attempts += 1
        source.last_request_at = now
        session.add(Attempt(job_id=job.id, number=job.attempts))
        session.flush()
        from .db import as_dict
        return {"job": as_dict(job), "feed": as_dict(session.get(Feed, job.feed_id)) if job.feed_id else None, "article": as_dict(session.get(Article, job.article_id)) if job.article_id else None}


def heartbeat(db, job_id, token):
    with db.session(immediate=True) as session:
        job = session.get(Job, job_id)
        if not job or job.lease_token != token or job.status != "running":
            return False
        job.heartbeat_at, job.lease_until = time.time(), time.time() + 180
        return not job.cancel_requested


def atomic_blob(settings, directory, content, suffix):
    digest = hashlib.sha256(content).hexdigest()
    relative = f"{directory}/{digest}{suffix}"
    path = settings.data_dir / relative
    if not path.exists():
        temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with temp.open("xb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    return digest, relative


def complete(db, job_id, token, result=None, error=None):
    now = time.time()
    with db.session(immediate=True) as session:
        job = session.get(Job, job_id)
        if not job or job.status != "running" or job.lease_token != token:
            return False
        source = session.get(Source, job.source_id)
        feed = session.get(Feed, job.feed_id) if job.feed_id else None
        article = session.get(Article, job.article_id) if job.article_id else None
        if job.cancel_requested:
            error = {"code": "CANCELLED", "message": "任务已取消"}
        job.lease_until = None
        if error:
            code = error["code"]
            message = error.get("message", code)
            job.error_code, job.error_message = code, message
            if article:
                article.last_error = message
                article.content_status = "saved" if article.content_hash else "pending"
                if code == "DELETED":
                    article.content_status = "deleted"
                elif code == "CONTENT_UNAVAILABLE":
                    article.content_status = "unavailable"
            if code in BLOCKS:
                block_source(session, source, code, message, job.id, error.get("retry_after", 0))
                job.status = BLOCKS[code]
            elif code == "NETWORK_ERROR" and job.attempts <= 2:
                source.network_failures += 1
                job.status = "retry_wait"
                job.run_after = now + (300 if job.attempts == 1 else 1800)
                if source.network_failures >= 3:
                    block_source(session, source, code, "来源连续网络失败，请检查网络后恢复", job.id)
                    job.status = "blocked_source"
            else:
                job.status = "cancelled" if code == "CANCELLED" else "failed"
                event_once(session, source, job.id, code, message)
            if job.kind == "archive_image":
                asset = session.scalar(select(Asset).where(Asset.article_id == article.id, Asset.source_url == job.payload["url"]))
                asset.status, asset.error_code = "failed", code
                article.media_status = "partial"
        else:
            source.network_failures = 0
            job.status, job.error_code, job.error_message = "succeeded", None, None
            job.active_key = None
            job.result = result or {}
            if job.kind == "discover":
                if not result.get("response_valid"):
                    raise ValueError("Discovery response was not validated")
                inserted = 0
                for item in result["items"]:
                    if job.payload.get("since") and item.get("published_at") and item["published_at"] < job.payload["since"]:
                        continue
                    found = session.scalar(select(Article).where(Article.feed_id == feed.id, Article.source_article_id == item["source_article_id"]))
                    canonical = canonical_url(item["url"])
                    if not found:
                        found = session.scalar(select(Article).where(Article.feed_id == feed.id, Article.canonical_url == canonical))
                    if not found:
                        found = Article(feed_id=feed.id, canonical_url=canonical, **item)
                        session.add(found)
                        session.flush()
                        inserted += 1
                    else:
                        found.title = item["title"]
                    if found.content_status == "pending":
                        enqueue(session, kind="fetch", feed_id=feed.id, article_id=found.id, priority=30)
                feed.last_check_at, feed.coverage = now, result["coverage"]
                if inserted:
                    feed.last_new_at = now
                remaining = job.payload.get("remaining", 1) - 1
                history = job.payload.get("history", False)
                dated = [item.get("published_at") for item in result["items"]]
                range_complete = bool(job.payload.get("since") and dated and all(value and value < job.payload["since"] for value in dated))
                if history:
                    feed.history_cursor = result["next_cursor"]
                job.result = {"discovered": len(result["items"]), "inserted": inserted, "next_cursor": result["next_cursor"], "coverage": result["coverage"], "exhausted": result["exhausted"], "range_complete": range_complete}
                if not result["exhausted"] and not range_complete and remaining > 0:
                    enqueue(session, kind="discover", feed_id=feed.id, payload={**job.payload, "offset": result["next_cursor"], "remaining": remaining}, priority=80 if history else 40, key=f"continue:{job.id}")
                elif not result["exhausted"] and not range_complete:
                    job.status = "partial"
                    job.result = {**job.result, "continuation_required": True}
            elif job.kind in ("fetch", "import"):
                if job.kind == "import":
                    url = job.payload["url"]
                    article = session.scalar(select(Article).where(Article.feed_id == feed.id, Article.canonical_url == canonical_url(url)))
                    if not article:
                        article = Article(feed_id=feed.id, source_article_id=hashlib.sha256(canonical_url(url).encode()).hexdigest(), url=url, canonical_url=canonical_url(url), title=job.payload.get("title") or "导入文章")
                        session.add(article)
                        session.flush()
                    job.article_id = article.id
                digest, raw_path = atomic_blob(db.settings, "archive", result["raw"].encode(), ".raw.html")
                _, clean_path = atomic_blob(db.settings, "archive", result["html"].encode(), ".html")
                old = session.scalar(select(Version).where(Version.article_id == article.id, Version.content_hash == digest))
                if not old:
                    session.add(Version(article_id=article.id, content_hash=digest, raw_path=raw_path, clean_path=clean_path))
                article.content_hash, article.content_html, article.plain_text = digest, result["html"], result["text"]
                article.content_status, article.last_success_at, article.last_error = "saved", now, None
                article.media_status = "remote" if result["images"] else "none"
                if result.get("format") == "text_only":
                    article.media_status = "unknown"
                if feed.archive_images and result["images"]:
                    article.media_status = "pending"
                    for url in result["images"]:
                        asset = session.scalar(select(Asset).where(Asset.article_id == article.id, Asset.source_url == url))
                        if not asset:
                            asset = Asset(article_id=article.id, source_url=url)
                            session.add(asset)
                        if asset.status != "saved":
                            enqueue(session, kind="archive_image", feed_id=feed.id, article_id=article.id, payload={"url": url}, priority=60)
                job.result = {"article_id": article.id, "content_hash": digest}
            elif job.kind == "archive_image":
                _, path = atomic_blob(db.settings, "assets", base64.b64decode(result["bytes"]), "." + {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}[result["mime"]])
                asset = session.scalar(select(Asset).where(Asset.article_id == article.id, Asset.source_url == job.payload["url"]))
                asset.status, asset.local_path, asset.error_code = "saved", path, None
                session.flush()
                assets = list(session.scalars(select(Asset).where(Asset.article_id == article.id)))
                article.media_status = "saved" if all(a.status == "saved" for a in assets) else "partial"
                job.result = {"asset_id": asset.id}
        if job.status in TERMINAL:
            job.active_key, job.ended_at = None, now
        attempt = session.scalar(select(Attempt).where(Attempt.job_id == job.id, Attempt.number == job.attempts))
        if attempt:
            attempt.outcome, attempt.error_code, attempt.ended_at = job.status, job.error_code, now
    return True
