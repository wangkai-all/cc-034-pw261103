"""Test fixtures: isolated settings, temp dirs, and a deterministic worker API."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.delivery import remove_tree  # noqa: E402
from app.worker import TaskWorker  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        scan_interval_seconds=0.2,
        max_single_file_bytes=32 * 1024 * 1024,
        max_total_expanded_bytes=48 * 1024 * 1024,
        max_files_per_package=200,
    )
    settings.ensure_dirs()
    return settings


@pytest.fixture()
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()
    remove_tree(settings.data_dir)


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    path = tmp_path / "incoming"
    path.mkdir()
    return path


@pytest.fixture()
def worker_factory(settings: Settings, db: Database):
    workers: list[TaskWorker] = []

    def make(source_dir: Path) -> TaskWorker:
        import json

        from app.worker import utcnow

        cur = db.execute(
            "INSERT INTO tasks (source_dir, status, created_at, config_json,"
            " progress_json) VALUES (?, 'paused', ?, ?, '{}')",
            (
                str(source_dir),
                utcnow(),
                json.dumps(
                    {
                        "max_single_file_bytes": settings.max_single_file_bytes,
                        "max_total_expanded_bytes": settings.max_total_expanded_bytes,
                        "max_files_per_package": settings.max_files_per_package,
                        "max_manifest_bytes": settings.max_manifest_bytes,
                        "scan_interval_seconds": settings.scan_interval_seconds,
                    }
                ),
            ),
        )
        worker = TaskWorker(int(cur.lastrowid), settings, db)
        workers.append(worker)
        return worker

    yield make

    for worker in workers:
        worker.stop()
