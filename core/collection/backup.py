import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
import portalocker


def backup(settings, target):
    destination = Path(target).resolve()
    if destination.exists():
        raise ValueError("备份目标必须是不存在的新目录")
    target = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.incomplete")
    target.mkdir(parents=True, mode=0o700)
    with portalocker.Lock(str(settings.data_dir / "source.lock"), timeout=120):
        with sqlite3.connect(settings.data_dir / "collector.db") as src, sqlite3.connect(target / "collector.db") as dst:
            src.backup(dst)
        with sqlite3.connect(target / "collector.db") as snapshot:
            paths = {p for row in snapshot.execute("SELECT raw_path, clean_path FROM article_versions") for p in row}
            paths.update(row[0] for row in snapshot.execute("SELECT local_path FROM article_assets WHERE status='saved' AND local_path IS NOT NULL"))
        for relative in paths:
            source = (settings.data_dir / relative).resolve()
            if not source.is_relative_to(settings.data_dir) or not source.is_file():
                raise ValueError("归档文件缺失或路径无效")
            file_destination = target / relative
            file_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, file_destination)
    files = {str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest() for path in target.rglob("*") if path.is_file()}
    (target / "manifest.json").write_text(json.dumps({"created_at": time.time(), "files": files, "auth_included": False}, indent=2))
    target.rename(destination)
    return {"files": len(files), "path": str(destination), "auth_included": False}


def restore(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists():
        raise ValueError("恢复目标必须是不存在的新目录")
    manifest = json.loads((source / "manifest.json").read_text())
    if "collector.db" not in manifest["files"]:
        raise ValueError("备份缺少数据库")
    for relative, digest in manifest["files"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("备份文件路径无效")
        path = (source / relative).resolve()
        if not path.is_relative_to(source) or path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("备份校验失败")
    target.mkdir(parents=True, mode=0o700)
    for relative in manifest["files"]:
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, dest)
    with sqlite3.connect(target / "collector.db") as conn:
        conn.execute("UPDATE source_accounts SET state='unconfigured',reason='恢复后请重新授权',paused_until=0 WHERE provider!='mock'")
        conn.execute("UPDATE collection_jobs SET status='queued',lease_token=NULL,lease_until=NULL WHERE status='running'")
        conn.commit()
    return {"path": str(target), "reauthorize": True}


def daily_backup(settings):
    """Keep 7 daily and 4 weekly snapshots in the app-owned backup directory."""
    import os
    root = Path(os.getenv("COLLECTOR_BACKUP_DIR", str(settings.data_dir / "backups"))).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now().strftime("%Y-%m-%d")
    target = root / name
    if not target.exists():
        backup(settings, target)
    candidates = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.is_symlink() or not (directory / "manifest.json").exists():
            continue
        try:
            day = datetime.strptime(directory.name, "%Y-%m-%d")
            manifest = json.loads((directory / "manifest.json").read_text())
            if "collector.db" not in manifest.get("files", {}):
                continue
        except (ValueError, OSError):
            continue
        candidates.append((day, directory))
    candidates.sort(reverse=True)
    keep = {directory for _, directory in candidates[:7]}
    weeks = set()
    for day, directory in candidates:
        week = day.isocalendar()[:2]
        if week not in weeks and len(weeks) < 4:
            weeks.add(week)
            keep.add(directory)
    for _, directory in candidates:
        if directory not in keep:
            shutil.rmtree(directory)
    return {"path": str(target), "retained": len(keep)}
