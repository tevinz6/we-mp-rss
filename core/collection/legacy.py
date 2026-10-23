"""Read-only import of an upstream We-MP-RSS SQLite archive; never imports credentials."""
from pathlib import Path
import sqlite3
import time
from sqlalchemy import select
from .adapters import SourceError, canonical_url, parse_body, validate_url
from .models import Article, Feed, Version
from .services import atomic_blob


def import_upstream(db, source):
    source = Path(source).resolve()
    if source == db.settings.data_dir / "collector.db" or not source.is_file():
        raise ValueError("请选择独立的原 We-MP-RSS SQLite 文件")
    legacy = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    legacy.row_factory = sqlite3.Row
    counts = {"feeds": 0, "articles": 0, "skipped": 0}
    try:
        tables = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"feeds", "articles"}.issubset(tables):
            raise ValueError("未识别为 We-MP-RSS 数据库")
        mapping = {}
        with db.session(immediate=True) as session:
            for raw in legacy.execute("SELECT * FROM feeds"):
                record = dict(raw)
                external = str(record.get("faker_id") or record["id"])
                feed = session.scalar(select(Feed).where(Feed.external_id == external, Feed.source_id == "primary"))
                if not feed:
                    feed = Feed(name=record.get("mp_name") or external, external_id=external, source_id="primary", enabled=False, coverage="imported")
                    session.add(feed)
                    session.flush()
                    counts["feeds"] += 1
                mapping[str(record["id"])] = feed.id
        for raw in legacy.execute("SELECT * FROM articles"):
            record = dict(raw)
            feed_id = mapping.get(str(record.get("mp_id")))
            url = record.get("url") or ""
            try:
                validate_url(url, resolve=False)
            except (SourceError, ValueError):
                counts["skipped"] += 1
                continue
            if not feed_id:
                counts["skipped"] += 1
                continue
            with db.session(immediate=True) as session:
                exists = session.scalar(select(Article).where(Article.feed_id == feed_id, Article.source_article_id == str(record["id"])))
                if exists:
                    counts["skipped"] += 1
                    continue
                published = record.get("publish_time")
                try:
                    published = float(published) if published else None
                    if published and published > 100000000000:
                        published /= 1000
                except (TypeError, ValueError):
                    published = None
                article = Article(feed_id=feed_id, source_article_id=str(record["id"]), title=record.get("title") or "导入文章", url=url, canonical_url=canonical_url(url), published_at=published)
                session.add(article)
                session.flush()
                content = record.get("content") or record.get("content_html") or ""
                if content:
                    try:
                        parsed = parse_body('<div id="js_content">' + content + '</div>')
                        digest, raw_path = atomic_blob(db.settings, "archive", parsed["raw"].encode(), ".raw.html")
                        _, clean_path = atomic_blob(db.settings, "archive", parsed["html"].encode(), ".html")
                        session.add(Version(article_id=article.id, content_hash=digest, raw_path=raw_path, clean_path=clean_path))
                        article.content_html, article.plain_text, article.content_hash = parsed["html"], parsed["text"], digest
                        article.content_status, article.last_success_at = "saved", time.time()
                        article.media_status = "remote" if parsed["images"] else "none"
                    except SourceError:
                        article.last_error = "导入内容不符合正文结构，等待重新获取"
                counts["articles"] += 1
    finally:
        legacy.close()
    return counts
