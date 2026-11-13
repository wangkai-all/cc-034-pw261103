"""Application settings, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser().resolve()
    return default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，当前为 {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"环境变量 {name} 必须 >= {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.2) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数字，当前为 {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"环境变量 {name} 必须 >= {minimum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / ".data")
    db_path: Path | None = None
    publish_dir: Path | None = None
    staging_dir: Path | None = None
    frontend_dist: Path = field(default_factory=lambda: REPO_ROOT / "frontend" / "dist")
    scan_interval_seconds: float = 5.0
    max_single_file_bytes: int = 32 * 1024 * 1024
    max_total_expanded_bytes: int = 128 * 1024 * 1024
    max_files_per_package: int = 500
    max_manifest_bytes: int = 8 * 1024 * 1024
    auto_scan: bool = True
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        if self.db_path is None:
            self.db_path = self.data_dir / "app.sqlite3"
        if self.publish_dir is None:
            self.publish_dir = self.data_dir / "published"
        if self.staging_dir is None:
            self.staging_dir = self.data_dir / "staging"
        self.db_path = Path(self.db_path).expanduser().resolve()
        self.publish_dir = Path(self.publish_dir).expanduser().resolve()
        self.staging_dir = Path(self.staging_dir).expanduser().resolve()
        self.frontend_dist = Path(self.frontend_dist).expanduser().resolve()

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.publish_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = _env_path("APP_DATA_DIR", REPO_ROOT / ".data")
        default_publish = data_dir / "published"
        default_staging = data_dir / "staging"
        return cls(
            data_dir=data_dir,
            db_path=_env_path("APP_DB_PATH", data_dir / "app.sqlite3"),
            publish_dir=_env_path("APP_PUBLISH_DIR", default_publish),
            staging_dir=_env_path("APP_STAGING_DIR", default_staging),
            frontend_dist=_env_path("APP_FRONTEND_DIST", REPO_ROOT / "frontend" / "dist"),
            scan_interval_seconds=_env_float("SCAN_INTERVAL_SECONDS", 5.0),
            max_single_file_bytes=_env_int(
                "MAX_SINGLE_FILE_BYTES", 32 * 1024 * 1024
            ),
            max_total_expanded_bytes=_env_int(
                "MAX_TOTAL_EXPANDED_BYTES", 128 * 1024 * 1024
            ),
            max_files_per_package=_env_int("MAX_FILES_PER_PACKAGE", 500),
            max_manifest_bytes=_env_int("MAX_MANIFEST_BYTES", 8 * 1024 * 1024),
            auto_scan=_env_bool("AUTO_SCAN", True),
        )


def format_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
