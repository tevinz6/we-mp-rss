from dataclasses import dataclass, field
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("COLLECTOR_DATA_DIR", "data/collector")).resolve())
    mock: bool = field(default_factory=lambda: os.getenv("COLLECTOR_MOCK", "false").lower() == "true")
    request_interval: float = field(default_factory=lambda: float(os.getenv("COLLECTOR_REQUEST_INTERVAL_SECONDS", "20")))
    timeout: float = field(default_factory=lambda: float(os.getenv("COLLECTOR_JOB_TIMEOUT_SECONDS", "90")))
    browser: bool = field(default_factory=lambda: os.getenv("COLLECTOR_BROWSER", "false").lower() == "true")
    secure_cookie: bool = field(default_factory=lambda: os.getenv("COLLECTOR_SECURE_COOKIE", "false").lower() == "true")

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("archive", "assets", "auth", "logs"):
            (self.data_dir / name).mkdir(exist_ok=True, mode=0o700)
        self.request_interval = max(0 if self.mock else 20, self.request_interval)

    @property
    def db_url(self):
        return f"sqlite:///{self.data_dir / 'collector.db'}"
