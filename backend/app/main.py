"""FastAPI application: task lifecycle, package inspection, batches, conflicts."""

from __future__ import annotations

import json
import mimetypes
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import Settings, format_bytes
from .db import Database
from .delivery import (
    MANIFEST_NAME,
    atomic_publish,
    remove_tree,
    resolve_source_dir,
)
from .worker import ACTIVE_TASK_STATES, WorkerRegistry, utcnow

MAX_PREVIEW_BYTES = 2 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".tsv", ".log", ".xml", ".yaml", ".yml",
    ".ini", ".toml", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
    ".sh", ".sql", ".rst", ".cfg", ".conf", ".properties",
}

PACKAGE_STATE_LABELS = {
    "detected": "已发现",
    "waiting_stable": "等待稳定",
    "receiving": "接收中",
    "received": "已接收",
    "validating": "核对中",
    "extracting": "解压中",
    "verifying": "校验中",
    "publishing": "发布中",
    "published": "已发布",
    "linked": "已关联",
    "failed": "已失败",
    "conflict": "冲突",
    "discarded": "已丢弃",
}

BATCH_STATE_LABELS = {
    "publishing": "发布中",
    "published": "已发布",
    "conflict": "待处理冲突",
    "failed": "发布失败",
    "discarded": "已丢弃",
    "superseded": "已替换",
}


class CreateTask(BaseModel):
    directory: str = Field(..., min_length=1, max_length=4096)
    allow_broad_directory: bool = False

    @field_validator("directory")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class ResolveConflict(BaseModel):
    resolution: str = Field(..., pattern="^(keep_existing|receive_incoming)$")


class Recheck(BaseModel):
    pass


def _json_detail(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _progress(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _check_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"total": len(rows), "ok": 0, "failed": 0}
    for row in rows:
        if row["result"] == "ok":
            summary["ok"] += 1
        else:
            summary["failed"] += 1
    return summary


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    db = db or Database(settings.db_path)
    registry = WorkerRegistry(settings, db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        rows = db.query(
            "SELECT id, status FROM tasks WHERE status IN ('running', 'pausing')"
        )
        for row in rows:
            db.execute(
                "UPDATE tasks SET status='running' WHERE id=?", (row["id"],)
            )
            registry.start(row["id"])
        try:
            yield
        finally:
            registry.stop_all()
            db.close()

    app = FastAPI(title="交付包接收台", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.registry = registry

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- helpers ---------------------------------------------------------
    def task_or_404(task_id: int) -> dict[str, Any]:
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task

    def package_or_404(package_id: int) -> dict[str, Any]:
        package = db.query_one("SELECT * FROM packages WHERE id=?", (package_id,))
        if package is None:
            raise HTTPException(status_code=404, detail="交付包不存在")
        return package

    def batch_or_404(batch_id: int) -> dict[str, Any]:
        batch = db.query_one("SELECT * FROM batches WHERE id=?", (batch_id,))
        if batch is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        return batch

    def task_summary(task: dict[str, Any]) -> dict[str, Any]:
        counts = {
            row["state"]: row["n"]
            for row in db.query(
                "SELECT state, COUNT(*) AS n FROM packages WHERE task_id=? GROUP BY state",
                (task["id"],),
            )
        }
        batches = {
            row["state"]: row["n"]
            for row in db.query(
                "SELECT state, COUNT(*) AS n FROM batches WHERE task_id=? GROUP BY state",
                (task["id"],),
            )
        }
        progress = _progress(task.get("progress_json"))
        config = json.loads(task["config_json"])
        return {
            "id": task["id"],
            "source_dir": task["source_dir"],
            "status": task["status"],
            "created_at": task["created_at"],
            "finished_at": task["finished_at"],
            "last_scan_at": task["last_scan_at"],
            "scan_error": task["scan_error"],
            "progress": progress,
            "package_counts": counts,
            "batch_counts": batches,
            "limits": {
                "single_file_bytes": config["max_single_file_bytes"],
                "single_file_label": format_bytes(config["max_single_file_bytes"]),
                "total_expanded_bytes": config["max_total_expanded_bytes"],
                "total_expanded_label": format_bytes(config["max_total_expanded_bytes"]),
                "max_files": config["max_files_per_package"],
                "scan_interval_seconds": config["scan_interval_seconds"],
            },
            "publish_dir": str(settings.publish_dir),
        }

    def package_summary(package: dict[str, Any]) -> dict[str, Any]:
        entries = db.query(
            "SELECT rel_path, result FROM manifest_entries WHERE package_id=?"
            " ORDER BY sort_order",
            (package["id"],),
        )
        return {
            "id": package["id"],
            "key": package["package_key"],
            "relative_path": package["relative_path"],
            "package_code": package["package_code"],
            "state": package["state"],
            "state_label": PACKAGE_STATE_LABELS.get(package["state"], package["state"]),
            "failure_code": package["failure_code"],
            "reason": package["reason"],
            "zip_name": package["zip_name"],
            "zip_sha256": package["zip_sha256"],
            "zip_bytes": package["zip_bytes"],
            "member_count": package["member_count"],
            "total_uncompressed": package["total_uncompressed"],
            "batch_id": package["batch_id"],
            "progress": _progress(package.get("progress_json")),
            "objects": _json_detail(package.get("objects_json")) or [],
            "checks": _check_summary(entries),
            "first_seen_at": package["first_seen_at"],
            "updated_at": package["updated_at"],
        }

    def batch_summary(batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": batch["id"],
            "package_code": batch["package_code"],
            "state": batch["state"],
            "state_label": BATCH_STATE_LABELS.get(batch["state"], batch["state"]),
            "version": batch["version"],
            "file_count": batch["file_count"],
            "total_bytes": batch["total_bytes"],
            "zip_name": batch["zip_name"],
            "zip_sha256": batch["zip_sha256"],
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "publish_path": batch["publish_path"],
            "publish_reason": batch["publish_reason"],
        }

    # -- routes ----------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "time": utcnow()}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "publish_dir": str(settings.publish_dir),
            "staging_dir": str(settings.staging_dir),
            "limits": {
                "single_file_bytes": settings.max_single_file_bytes,
                "single_file_label": format_bytes(settings.max_single_file_bytes),
                "total_expanded_bytes": settings.max_total_expanded_bytes,
                "total_expanded_label": format_bytes(settings.max_total_expanded_bytes),
                "max_files": settings.max_files_per_package,
                "manifest_bytes": settings.max_manifest_bytes,
                "scan_interval_seconds": settings.scan_interval_seconds,
            },
            "preview_limit_bytes": MAX_PREVIEW_BYTES,
        }

    @app.get("/api/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        rows = db.query("SELECT * FROM tasks ORDER BY id DESC")
        return [task_summary(row) for row in rows]

    @app.post("/api/tasks/{task_id}/scan")
    def scan_now(task_id: int) -> dict[str, Any]:
        """Trigger one scan pass immediately (no-op when the task is paused)."""
        task_or_404(task_id)
        worker = registry.get(task_id)
        if worker is not None and worker.is_alive():
            worker.wake()
            return {"ok": True, "triggered": True}
        return {"ok": True, "triggered": False}

    @app.post("/api/tasks", status_code=201)
    def create_task(payload: CreateTask) -> dict[str, Any]:
        try:
            directory = resolve_source_dir(payload.directory)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if directory == Path("/"):
            raise HTTPException(
                status_code=400,
                detail="拒绝直接扫描文件系统根目录，请指定专门的接收目录。",
            )
        broad = directory in (Path("/workspace"), Path.home().resolve())
        if broad and not payload.allow_broad_directory:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{directory} 范围过宽，可能包含无关目录。"
                    "确认要使用请勾选“允许扫描范围较宽的目录”。"
                ),
            )
        for guard in (settings.data_dir, settings.staging_dir, settings.publish_dir):
            if directory == guard or guard in directory.parents:
                raise HTTPException(
                    status_code=400,
                    detail=f"不能扫描本应用自身的数据目录：{directory}",
                )

        existing = db.query_one(
            "SELECT id, status FROM tasks WHERE source_dir=? AND status IN ('running','pausing')",
            (str(directory),),
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"该目录已有运行中的任务（#{existing['id']}），请在任务列表中查看。",
            )

        config = {
            "max_single_file_bytes": settings.max_single_file_bytes,
            "max_total_expanded_bytes": settings.max_total_expanded_bytes,
            "max_files_per_package": settings.max_files_per_package,
            "max_manifest_bytes": settings.max_manifest_bytes,
            "scan_interval_seconds": settings.scan_interval_seconds,
        }
        cur = db.execute(
            "INSERT INTO tasks (source_dir, status, created_at, config_json,"
            " progress_json) VALUES (?, 'running', ?, ?, ?)",
            (
                str(directory),
                utcnow(),
                json.dumps(config, ensure_ascii=False),
                json.dumps(
                    {"scanning": {"status": "starting", "message": None}},
                    ensure_ascii=False,
                ),
            ),
        )
        task_id = int(cur.lastrowid)
        db.add_event(
            task_id,
            "info",
            "task_created",
            f"创建接收任务，扫描目录 {directory}",
            created_at=utcnow(),
        )
        registry.start(task_id)
        return task_summary(task_or_404(task_id))

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> dict[str, Any]:
        return task_summary(task_or_404(task_id))

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(task_id: int) -> dict[str, Any]:
        task = task_or_404(task_id)
        if task["status"] == "paused":
            return task_summary(task)
        registry.stop(task_id, wait=True)
        db.execute(
            "UPDATE tasks SET status='paused', finished_at=? WHERE id=?",
            (utcnow(), task_id),
        )
        return task_summary(task_or_404(task_id))

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: int) -> dict[str, Any]:
        task = task_or_404(task_id)
        if task["status"] in ACTIVE_TASK_STATES:
            return task_summary(task)
        db.execute(
            "UPDATE tasks SET status='running', finished_at=NULL WHERE id=?",
            (task_id,),
        )
        registry.start(task_id)
        return task_summary(task_or_404(task_id))

    @app.post("/api/tasks/{task_id}/rescan")
    def rescan(task_id: int) -> dict[str, Any]:
        task = task_or_404(task_id)
        if task["status"] not in ACTIVE_TASK_STATES:
            # Reset failed/terminal packages so the next round re-evaluates them.
            db.execute(
                "UPDATE packages SET state='detected', failure_code=NULL,"
                " reason='用户触发重新扫描，重新评估。', source_fingerprint=NULL,"
                " updated_at=? WHERE task_id=? AND state='failed'",
                (utcnow(), task_id),
            )
            db.execute(
                "UPDATE tasks SET status='running', finished_at=NULL WHERE id=?",
                (task_id,),
            )
            registry.start(task_id)
        else:
            registry.wake(task_id)
        return task_summary(task_or_404(task_id))

    @app.get("/api/tasks/{task_id}/packages")
    def list_packages(task_id: int) -> list[dict[str, Any]]:
        task_or_404(task_id)
        rows = db.query(
            "SELECT * FROM packages WHERE task_id=? ORDER BY id", (task_id,)
        )
        return [package_summary(row) for row in rows]

    @app.get("/api/packages/{package_id}")
    def get_package(package_id: int) -> dict[str, Any]:
        package = package_or_404(package_id)
        task = task_or_404(package["task_id"])
        summary = package_summary(package)
        summary["source_dir"] = task["source_dir"]
        summary["entries"] = db.query(
            "SELECT rel_path, expected_bytes, expected_sha256, actual_bytes,"
            " actual_sha256, result, reason, sort_order FROM manifest_entries"
            " WHERE package_id=? ORDER BY sort_order",
            (package_id,),
        )
        summary["batch"] = (
            batch_summary(batch_or_404(package["batch_id"]))
            if package["batch_id"]
            else None
        )
        return summary

    @app.get("/api/tasks/{task_id}/batches")
    def list_batches(task_id: int) -> list[dict[str, Any]]:
        task_or_404(task_id)
        rows = db.query(
            "SELECT * FROM batches WHERE task_id=? ORDER BY id DESC", (task_id,)
        )
        return [batch_summary(row) for row in rows]

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: int) -> dict[str, Any]:
        batch = batch_or_404(batch_id)
        summary = batch_summary(batch)
        summary["files"] = db.query(
            "SELECT id, rel_path, byte_size, sha256, result, reason, sort_order"
            " FROM batch_files WHERE batch_id=? ORDER BY sort_order, rel_path",
            (batch_id,),
        )
        summary["zip_download_url"] = (
            f"/api/batches/{batch_id}/zip"
            if batch["state"] in {"published", "conflict", "superseded"}
            else None
        )
        summary["publish_reason"] = batch["publish_reason"]
        return summary

    def _batch_file(batch: dict[str, Any], file_id: int) -> dict[str, Any]:
        row = db.query_one(
            "SELECT * FROM batch_files WHERE batch_id=? AND id=?", (batch["id"], file_id)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        return row

    def _safe_published_path(batch: dict[str, Any], rel_path: str) -> Path | None:
        """Resolve a file under the published dir, or the staging dir before publish."""
        roots: list[Path] = []
        if batch["publish_path"]:
            roots.append(Path(batch["publish_path"]))
        if batch["source_package_id"]:
            roots.append(
                settings.staging_dir
                / f"task-{batch['task_id']}"
                / f"pkg-{batch['source_package_id']}"
                / "extract"
            )
        for root in roots:
            candidate = (root / Path(rel_path)).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    @app.get("/api/batches/{batch_id}/files/{file_id}/preview")
    def preview_file(batch_id: int, file_id: int) -> dict[str, Any]:
        batch = batch_or_404(batch_id)
        row = _batch_file(batch, file_id)
        if row["result"] != "ok":
            raise HTTPException(status_code=409, detail="该文件未通过校验，不能预览")
        path = _safe_published_path(batch, row["rel_path"])
        if path is None:
            raise HTTPException(status_code=404, detail="文件已不可用（可能已被清理）")
        size = path.stat().st_size
        suffix = Path(row["rel_path"]).suffix.lower()
        if suffix in TEXT_EXTENSIONS:
            if size > MAX_PREVIEW_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件 {format_bytes(size)} 超过预览上限 {format_bytes(MAX_PREVIEW_BYTES)}，请直接下载。",
                )
            try:
                text = path.read_text(encoding="utf-8")
                truncated = False
            except UnicodeDecodeError:
                return {
                    "kind": "binary",
                    "message": "二进制文件不支持页内预览，请下载查看。",
                    "size": size,
                }
            return {
                "kind": "text",
                "content": text,
                "size": size,
                "truncated": truncated,
                "encoding": "utf-8",
            }
        return {
            "kind": "binary",
            "message": "该文件类型不支持页内预览，请下载查看。",
            "size": size,
            "mime": mimetypes.guess_type(row["rel_path"])[0] or "application/octet-stream",
        }

    @app.get("/api/batches/{batch_id}/files/{file_id}/download")
    def download_file(batch_id: int, file_id: int) -> FileResponse:
        batch = batch_or_404(batch_id)
        row = _batch_file(batch, file_id)
        if row["result"] != "ok":
            raise HTTPException(status_code=409, detail="该文件未通过校验，不能下载")
        path = _safe_published_path(batch, row["rel_path"])
        if path is None:
            raise HTTPException(status_code=404, detail="文件已不可用（可能已被清理）")
        return FileResponse(
            path,
            media_type=mimetypes.guess_type(row["rel_path"])[0]
            or "application/octet-stream",
            filename=Path(row["rel_path"]).name,
        )

    @app.get("/api/batches/{batch_id}/zip")
    def download_zip(batch_id: int) -> FileResponse:
        batch = batch_or_404(batch_id)
        if not batch["zip_name"]:
            raise HTTPException(status_code=404, detail="压缩包不可用")
        path: Path | None = None
        if batch["publish_path"]:
            candidate = Path(batch["publish_path"]) / batch["zip_name"]
            if candidate.is_file():
                path = candidate
        if path is None and batch["source_package_id"]:
            candidate = (
                settings.staging_dir
                / f"task-{batch['task_id']}"
                / f"pkg-{batch['source_package_id']}"
                / batch["zip_name"]
            )
            if candidate.is_file():
                path = candidate
        if path is None:
            raise HTTPException(status_code=404, detail="压缩包已不可用")
        return FileResponse(
            path, media_type="application/zip", filename=batch["zip_name"]
        )

    @app.get("/api/tasks/{task_id}/events")
    def list_events(
        task_id: int, limit: int = Query(default=100, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        task_or_404(task_id)
        rows = db.query(
            "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        )
        for row in rows:
            row["detail"] = _json_detail(row.pop("detail_json", None))
        return rows

    # -- conflict resolution --------------------------------------------
    @app.get("/api/tasks/{task_id}/conflicts")
    def list_conflicts(task_id: int) -> list[dict[str, Any]]:
        task_or_404(task_id)
        rows = db.query(
            "SELECT * FROM packages WHERE task_id=? AND state='conflict' ORDER BY id",
            (task_id,),
        )
        conflicts: list[dict[str, Any]] = []
        for package in rows:
            summary = package_summary(package)
            batch = (
                batch_or_404(package["batch_id"]) if package["batch_id"] else None
            )
            existing = None
            if batch:
                existing = db.query_one(
                    "SELECT * FROM batches WHERE task_id=? AND package_code=?"
                    " AND id != ? AND state IN ('published','publishing','conflict')"
                    " ORDER BY id DESC",
                    (task_id, batch["package_code"], batch["id"]),
                )
            summary["incoming_batch"] = batch_summary(batch) if batch else None
            summary["existing_batch"] = (
                batch_summary(existing) if existing else None
            )
            conflicts.append(summary)
        return conflicts

    @app.post("/api/packages/{package_id}/resolve")
    def resolve_conflict(package_id: int, payload: ResolveConflict) -> dict[str, Any]:
        package = package_or_404(package_id)
        if package["state"] != "conflict":
            raise HTTPException(status_code=409, detail="该交付包当前不是冲突状态")
        batch = batch_or_404(package["batch_id"]) if package["batch_id"] else None
        if batch is None:
            raise HTTPException(status_code=409, detail="冲突批次缺失，无法处理")
        now = utcnow()

        if payload.resolution == "keep_existing":
            db.execute(
                "UPDATE batches SET state='discarded', publish_reason=?, updated_at=?"
                " WHERE id=?",
                ("用户选择保留已有批次。", now, batch["id"]),
            )
            db.execute(
                "UPDATE packages SET state='discarded', failure_code=NULL,"
                " reason='已按用户选择保留原有批次，本次内容不发布。', updated_at=?"
                " WHERE id=?",
                (now, package_id),
            )
            remove_tree(settings.staging_dir / f"task-{package['task_id']}"
                        / f"pkg-{package_id}")
            db.add_event(
                package["task_id"],
                "info",
                "conflict_resolved",
                f"冲突已处理：保留批次 #{batch['id']} 的已有内容，丢弃本次内容。",
                package_id=package_id,
                batch_id=batch["id"],
                created_at=now,
            )
            return {"ok": True, "resolution": "keep_existing", "batch_id": batch["id"]}

        # Promote the incoming batch and atomically replace the published dir.
        staging_extract = (
            settings.staging_dir
            / f"task-{package['task_id']}"
            / f"pkg-{package_id}"
            / "extract"
        )
        if not staging_extract.is_dir():
            raise HTTPException(
                status_code=409,
                detail="本次内容的暂存文件已不可用，请重新扫描源目录后再处理。",
            )
        published = settings.publish_dir / batch["package_code"]
        build = settings.staging_dir / f"task-{package['task_id']}" / f"batch-{batch['id']}"
        backup = settings.publish_dir / f".superseded-{batch['package_code']}-{batch['id']}"
        remove_tree(build)
        try:
            build.mkdir(parents=True)
            for dirpath, dirnames, filenames in _walk(staging_extract):
                rel = Path(dirpath).relative_to(staging_extract)
                dest_dir = build / rel
                dest_dir.mkdir(parents=True, exist_ok=True)
                for name in filenames:
                    shutil.copy2(Path(dirpath) / name, dest_dir / name)
            source_pkg = settings.staging_dir / f"task-{package['task_id']}" / f"pkg-{package_id}"
            shutil.copy2(source_pkg / MANIFEST_NAME, build / MANIFEST_NAME)
            shutil.copy2(source_pkg / batch["zip_name"], build / batch["zip_name"])
        except Exception as exc:
            remove_tree(build)
            raise HTTPException(status_code=500, detail=f"组装发布目录失败：{exc}") from exc

        moved_existing = False
        try:
            if published.exists():
                remove_tree(backup)
                published.rename(backup)
                moved_existing = True
            atomic_publish(build, published)
        except Exception as exc:
            remove_tree(build)
            if moved_existing and not published.exists() and backup.exists():
                backup.rename(published)
            raise HTTPException(
                status_code=500, detail=f"替换发布目录失败：{exc}（已尽量回滚）"
            ) from exc
        remove_tree(backup)

        previous = db.query_one(
            "SELECT id FROM batches WHERE task_id=? AND package_code=? AND id != ?"
            " AND state IN ('published','publishing') ORDER BY id DESC",
            (package["task_id"], batch["package_code"], batch["id"]),
        )
        if previous:
            db.execute(
                "UPDATE batches SET state='superseded', publish_reason=?, updated_at=?"
                " WHERE id=?",
                ("已被批次 #%d 替换。" % batch["id"], now, previous["id"]),
            )
        db.execute(
            "UPDATE batches SET state='published', publish_path=?, publish_reason=NULL,"
            " updated_at=? WHERE id=?",
            (str(published), now, batch["id"]),
        )
        db.execute(
            "UPDATE batch_files SET full_path=replace(full_path, ?, ?) WHERE batch_id=?",
            (str(staging_extract), str(published), batch["id"]),
        )
        db.execute(
            "UPDATE packages SET state='published', failure_code=NULL,"
            " reason=?, updated_at=? WHERE id=?",
            (f"冲突已处理：已用本批次替换原有内容，发布到 {published}", now, package_id),
        )
        db.add_event(
            package["task_id"],
            "info",
            "conflict_resolved",
            f"冲突已处理：用批次 #{batch['id']} 的内容替换 {batch['package_code']}。",
            package_id=package_id,
            batch_id=batch["id"],
            created_at=now,
        )
        remove_tree(settings.staging_dir / f"task-{package['task_id']}" / f"pkg-{package_id}")
        return {"ok": True, "resolution": "receive_incoming", "batch_id": batch["id"]}

    # -- static frontend (built React app) ------------------------------
    if settings.frontend_dist.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(settings.frontend_dist), html=True),
            name="frontend",
        )
    else:

        @app.get("/")
        def _no_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "message": (
                        "前端尚未构建。请在 frontend 目录执行 `npm install && npm run build`，"
                        "或使用 `npm run dev` 进行开发。"
                    ),
                    "api": "/docs",
                },
                status_code=200,
            )

    return app


def _walk(root: Path):
    import os

    return os.walk(root, followlinks=False)


app = create_app()
