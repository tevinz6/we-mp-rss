import argparse
import getpass
import json
import logging
import os
from pathlib import Path
from sqlalchemy import select
from .config import Settings
from .db import Database
from .models import Admin, Feed
from .services import initialize, enqueue


def main():
    parser = argparse.ArgumentParser(description="公众号采集工作台")
    parser.add_argument("command", choices=["init", "serve", "worker", "demo", "backup", "restore", "reset-password", "import-upstream"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--target")
    parser.add_argument("--source")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "restore":
        if not args.source or not args.target:
            parser.error("restore requires --source and --target")
        from .backup import restore
        print(json.dumps(restore(args.source, args.target), ensure_ascii=False))
        return
    settings = Settings()
    db = Database(settings)
    initialize(db)
    if args.command == "init":
        print("初始化完成；启动网页后创建管理员密码。")
    elif args.command == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
    elif args.command == "worker":
        from .worker import Worker
        Worker(settings).run()
    elif args.command == "demo":
        if not settings.mock:
            parser.error("demo requires COLLECTOR_MOCK=true and a separate data directory")
        with db.session(immediate=True) as session:
            for name, external in [("科技观察 · 演示", "demo-tech"), ("产品手记 · 演示", "demo-product")]:
                feed = session.scalar(select(Feed).where(Feed.external_id == external))
                if not feed:
                    feed = Feed(name=name, source_id="primary", external_id=external)
                    session.add(feed)
                    session.flush()
                enqueue(session, kind="discover", feed_id=feed.id, payload={"offset": 0, "remaining": 2})
        from .worker import Worker
        worker = Worker(settings)
        while worker.once():
            pass
        print("本地演示完成；未请求微信。")
    elif args.command == "backup":
        if not args.target:
            parser.error("backup requires --target")
        from .backup import backup
        print(json.dumps(backup(settings, args.target), ensure_ascii=False))
    elif args.command == "import-upstream":
        if not args.source:
            parser.error("import-upstream requires --source")
        from .legacy import import_upstream
        print(json.dumps(import_upstream(db, args.source), ensure_ascii=False))
    elif args.command == "reset-password":
        from .security import hash_password
        password = getpass.getpass("新密码（至少 12 位）: ")
        if len(password) < 12 or password != getpass.getpass("再次输入: "):
            parser.error("密码长度不足或两次输入不一致")
        with db.session(immediate=True) as session:
            admin = session.get(Admin, 1)
            if admin:
                admin.password_hash = hash_password(password)
            else:
                session.add(Admin(id=1, password_hash=hash_password(password)))
        print("密码已更新。")


if __name__ == "__main__":
    main()
