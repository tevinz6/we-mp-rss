import time
import uuid
from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def uid():
    return str(uuid.uuid4())


class Source(Base):
    __tablename__ = "source_accounts"
    id = Column(String, primary_key=True)
    provider = Column(String, nullable=False)
    state = Column(String, default="unconfigured", nullable=False)
    reason = Column(String)
    paused_until = Column(Float, default=0)
    last_request_at = Column(Float, default=0)
    last_auth_at = Column(Float)
    network_failures = Column(Integer, default=0)


class Feed(Base):
    __tablename__ = "feeds"
    id = Column(String, primary_key=True, default=uid)
    source_id = Column(String, ForeignKey("source_accounts.id"), nullable=False)
    external_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    enabled = Column(Boolean, default=True)
    interval_minutes = Column(Integer, default=360)
    next_run_at = Column(Float, default=time.time)
    last_check_at = Column(Float)
    last_new_at = Column(Float)
    history_cursor = Column(Integer, default=0)
    coverage = Column(String, default="unverified")
    collection_mode = Column(String, default="list", server_default="list", nullable=False)
    archive_images = Column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)


class Article(Base):
    __tablename__ = "articles"
    id = Column(String, primary_key=True, default=uid)
    feed_id = Column(String, ForeignKey("feeds.id"), nullable=False, index=True)
    source_article_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    url = Column(Text, nullable=False)
    canonical_url = Column(Text, nullable=False)
    author = Column(String)
    published_at = Column(Float, index=True)
    discovered_at = Column(Float, default=time.time)
    content_status = Column(String, default="pending")
    media_status = Column(String, default="remote")
    content_html = Column(Text, default="")
    plain_text = Column(Text, default="")
    content_hash = Column(String)
    last_success_at = Column(Float)
    last_error = Column(String)
    __table_args__ = (UniqueConstraint("feed_id", "source_article_id"),)


class Job(Base):
    __tablename__ = "collection_jobs"
    id = Column(String, primary_key=True, default=uid)
    source_id = Column(String, ForeignKey("source_accounts.id"), nullable=False)
    feed_id = Column(String, ForeignKey("feeds.id"))
    article_id = Column(String, ForeignKey("articles.id"))
    kind = Column(String, nullable=False)
    status = Column(String, default="queued", index=True)
    priority = Column(Integer, default=50)
    idempotency_key = Column(String, unique=True, nullable=False)
    active_key = Column(String, unique=True)
    payload = Column(JSON, default=dict)
    result = Column(JSON, default=dict)
    attempts = Column(Integer, default=0)
    created_at = Column(Float, default=time.time)
    run_after = Column(Float, default=time.time, index=True)
    started_at = Column(Float)
    ended_at = Column(Float)
    lease_until = Column(Float)
    lease_token = Column(String)
    heartbeat_at = Column(Float)
    cancel_requested = Column(Boolean, default=False)
    error_code = Column(String)
    error_message = Column(String)


class Attempt(Base):
    __tablename__ = "collection_attempts"
    id = Column(Integer, primary_key=True)
    job_id = Column(String, ForeignKey("collection_jobs.id"), nullable=False, index=True)
    number = Column(Integer)
    started_at = Column(Float, default=time.time)
    ended_at = Column(Float)
    outcome = Column(String)
    error_code = Column(String)


class Version(Base):
    __tablename__ = "article_versions"
    id = Column(String, primary_key=True, default=uid)
    article_id = Column(String, ForeignKey("articles.id"), nullable=False)
    content_hash = Column(String, nullable=False)
    raw_path = Column(String, nullable=False)
    clean_path = Column(String, nullable=False)
    captured_at = Column(Float, default=time.time)
    __table_args__ = (UniqueConstraint("article_id", "content_hash"),)


class Asset(Base):
    __tablename__ = "article_assets"
    id = Column(Integer, primary_key=True)
    article_id = Column(String, ForeignKey("articles.id"), nullable=False)
    source_url = Column(Text, nullable=False)
    local_path = Column(String)
    status = Column(String, default="pending")
    error_code = Column(String)
    __table_args__ = (UniqueConstraint("article_id", "source_url"),)


class Event(Base):
    __tablename__ = "collection_events"
    id = Column(String, primary_key=True, default=uid)
    source_id = Column(String)
    job_id = Column(String)
    code = Column(String, nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(Float, default=time.time)
    resolved_at = Column(Float)


class Admin(Base):
    __tablename__ = "collection_admin"
    id = Column(Integer, primary_key=True)
    password_hash = Column(String, nullable=False)
