from contextlib import contextmanager
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker
from alembic import command
from alembic.config import Config
from .config import ROOT


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.engine = create_engine(settings.db_url, connect_args={"check_same_thread": False, "timeout": 5})

        @event.listens_for(self.engine, "connect")
        def configure(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self):
        import portalocker
        with portalocker.Lock(str(self.settings.data_dir / "migration.lock"), timeout=30):
            tables = set(inspect(self.engine).get_table_names())
            if tables and "alembic_version" not in tables:
                raise RuntimeError("Refusing an unmanaged database; use a new collector data directory.")
            config = Config()
            config.set_main_option("script_location", str(ROOT / "migrations_collector"))
            config.set_main_option("sqlalchemy.url", self.settings.db_url.replace("%", "%%"))
            command.upgrade(config, "head")

    @contextmanager
    def session(self, immediate=False):
        with self.factory() as session:
            try:
                if immediate:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise


def as_dict(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
