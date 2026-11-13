"""Background worker: scan, receive, verify, publish (one thread per task)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .delivery import (
    MANIFEST_NAME,
    STATE_EXTRACTING,
    STATE_PUBLISHING,
    STATE_RECEIVED,
    STATE_RECEIVING,
    STATE_VALIDATING,
    STATE_VERIFYING,
    STATE_WAITING,
    TERMINAL_PACKAGE_STATES,
    Failure,
    Manifest,
    atomic_publish,
    compare_manifest,
    copy_zip_to_staging,
    discover_packages,
    extract_and_hash,
    inspect_zip,
    load_manifest,
    peek_zip_name,
    read_snapshot,
    remove_tree,
    same_sizes,
    sha256_file,
    verify_extracted,
)

log = logging.getLogger("delivery.worker")

ACTIVE_TASK_STATES = {"running", "pausing"}
RESUME_STATES = {
    STATE_RECEIVED,
    STATE_VALIDATING,
    STATE_EXTRACTING,
    STATE_VERIFYING,
    STATE_PUBLISHING,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(snapshot: dict[str, tuple[int, int, int]]) -> str:
    return json.dumps(sorted(snapshot.items()), ensure_ascii=False)


class TaskWorker(threading.Thread):
    def __init__(self, task_id: int, settings: Settings, db: Database) -> None:
        super().__init__(name=f"task-{task_id}", daemon=True)
        self.task_id = task_id
        self.settings = settings
        self.db = db
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._scan_lock = threading.Lock()
        self._snapshots: dict[str, dict[str, tuple[int, int, int]]] = {}
        self._stable: dict[str, dict[str, tuple[int, int, int]]] = {}
        self._exhausted_state: str | None = None
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if task is None:
            raise ValueError(f"task {task_id} not found")
        self.source_dir = Path(task["source_dir"])
        self.task_dir = settings.staging_dir / f"task-{task_id}"

    # -- task helpers ----------------------------------------------------
    def update_task(self, **fields: Any) -> None:
        if not fields:
            return
        keys = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE tasks SET {keys} WHERE id=?", (*fields.values(), self.task_id)
        )

    def add_event(
        self,
        level: str,
        code: str,
        message: str,
        *,
        package_id: int | None = None,
        batch_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.db.add_event(
            self.task_id,
            level,
            code,
            message,
            package_id=package_id,
            batch_id=batch_id,
            detail=detail,
            created_at=utcnow(),
        )

    def set_progress(self, **fields: Any) -> None:
        row = self.db.query_one(
            "SELECT progress_json FROM tasks WHERE id=?", (self.task_id,)
        )
        current: dict[str, Any] = {}
        if row and row.get("progress_json"):
            try:
                current = json.loads(row["progress_json"])
            except ValueError:
                current = {}
        current.update(fields)
        self.update_task(progress_json=json.dumps(current, ensure_ascii=False))

    def counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT state, COUNT(*) AS n FROM packages WHERE task_id=? GROUP BY state",
            (self.task_id,),
        )
        return {row["state"]: row["n"] for row in rows}

    def flush_progress(self) -> None:
        task = self.db.query_one(
            "SELECT last_scan_at FROM tasks WHERE id=?", (self.task_id,)
        )
        self.set_progress(
            package_counts=self.counts(),
            scanned_at=task["last_scan_at"] if task else None,
        )

    # -- lifecycle -------------------------------------------------------
    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def run(self) -> None:  # pragma: no cover - loops forever while active
        try:
            self.task_dir.mkdir(parents=True, exist_ok=True)
            while not self._stop_event.is_set():
                try:
                    self.scan_round()
                except Exception:
                    log.exception("scan round failed for task %s", self.task_id)
                    self.add_event("error", "scan_crashed", "扫描轮次异常，已跳过本轮。")
                self._wake.wait(self.settings.scan_interval_seconds)
                self._wake.clear()
        finally:
            if self._stop_event.is_set():
                self.mark_finished()

    def mark_finished(self) -> None:
        task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (self.task_id,))
        if task and task["status"] in ACTIVE_TASK_STATES:
            self.update_task(status="paused", finished_at=utcnow())

    # -- scanning --------------------------------------------------------
    def scan_round(self, *, force: bool = False) -> None:
        """Run one scan pass.

        ``force`` lets a paused task perform a single pass (the manual rescan
        button), which is also how tests drive the worker deterministically.
        """
        with self._scan_lock:
            self._scan_round(force=force)

    def _scan_round(self, *, force: bool = False) -> None:
        task = self.db.query_one("SELECT status FROM tasks WHERE id=?", (self.task_id,))
        if task is None:
            return
        if not force and task["status"] not in ACTIVE_TASK_STATES:
            return
        self.set_progress(scanning={"status": "scanning", "message": None})
        try:
            candidates = discover_packages(self.source_dir)
        except FileNotFoundError:
            return self._scan_error("源目录不存在")
        except PermissionError:
            return self._scan_error("没有权限读取源目录")
        except OSError as exc:
            return self._scan_error(f"读取源目录失败：{exc}")

        for folder in candidates:
            if self._stop_event.is_set():
                return
            try:
                self._handle_folder(folder)
            except Exception:
                log.exception("package handling failed: %s", folder)
                self.add_event(
                    "error",
                    "internal_error",
                    f"处理交付目录 {folder.name} 时出现内部错误，正式数据未受影响。",
                )

        self.update_task(last_scan_at=utcnow(), scan_error=None)
        self.set_progress(
            scanning={
                "status": "ok",
                "candidates": len(candidates),
                "message": None,
            },
            last_scan_at=utcnow(),
            package_counts=self.counts(),
            current_package=None,
        )

    def _scan_error(self, message: str) -> None:
        self.update_task(last_scan_at=utcnow(), scan_error=message)
        self.set_progress(
            scanning={"status": "error", "message": message},
            scanned_at=utcnow(),
            package_counts=self.counts(),
        )

    def _handle_folder(self, folder: Path) -> None:
        key = folder.name
        snapshot = read_snapshot(folder)
        previous = self._snapshots.get(key)
        self._snapshots[key] = snapshot

        package = self._upsert_package(key, folder)
        if package["state"] in TERMINAL_PACKAGE_STATES:
            if package["source_fingerprint"] == _fingerprint(snapshot):
                return
            # Folder changed after a terminal outcome: treat as a new attempt.
            # A conflict keeps its staged copy so the user can still resolve it.
            self._reset_package(
                package,
                "交付目录内容发生变化，重新评估。",
                keep_staging=package["state"] == "conflict",
            )
            package = self.db.query_one(
                "SELECT * FROM packages WHERE id=?", (package["id"],)
            )

        if package["state"] in RESUME_STATES:
            # Resume interrupted work after a restart. ``receiving`` is not
            # resumed: a half-copied staging directory is discarded and the
            # copy starts again once the source is stable.
            staging = self.task_dir / f"pkg-{package['id']}"
            if (staging / MANIFEST_NAME).is_file():
                self._process_staged(package)
                return
            self._reset_package(package, "暂存副本缺失，将重新接收。")
            package = self.db.query_one(
                "SELECT * FROM packages WHERE id=?", (package["id"],)
            )
            if previous is not None and same_sizes(previous, snapshot):
                self._stable[key] = snapshot
                self._receive(package, folder)
            else:
                self.update_package(
                    package["id"],
                    state=STATE_WAITING,
                    reason="暂存副本缺失，等待源文件稳定后重新接收。",
                )
            return

        self.update_package(
            package["id"],
            source_fingerprint=_fingerprint(snapshot),
            objects_json=json.dumps(object_view(folder), ensure_ascii=False),
        )

        stable = previous is not None and same_sizes(previous, snapshot)
        if not stable:
            self._stable.pop(key, None)
            if package["state"] != STATE_WAITING:
                self.update_package(
                    package["id"],
                    state=STATE_WAITING,
                    failure_code=None,
                    reason="检测到文件仍在变化，等待连续两次扫描大小一致。",
                )
            else:
                self.update_package(
                    package["id"],
                    reason="检测到文件仍在变化，等待连续两次扫描大小一致。",
                )
            return

        self._stable[key] = snapshot
        self.update_package(
            package["id"],
            state=STATE_WAITING,
            failure_code=None,
            reason="文件大小已连续两次扫描不变，等待接收。",
        )
        self._receive(package, folder)

    def _upsert_package(self, key: str, folder: Path) -> dict[str, Any]:
        existing = self.db.query_one(
            "SELECT * FROM packages WHERE task_id=? AND package_key=?",
            (self.task_id, key),
        )
        if existing:
            self.update_package(existing["id"], relative_path=key)
            return self.db.query_one(
                "SELECT * FROM packages WHERE id=?", (existing["id"],)
            )
        cur = self.db.execute(
            "INSERT INTO packages (task_id, package_key, relative_path, state,"
            " first_seen_at, updated_at) VALUES (?, ?, ?, 'detected', ?, ?)",
            (self.task_id, key, key, utcnow(), utcnow()),
        )
        self.add_event(
            "info",
            "package_discovered",
            f"发现候选交付目录 {key}。",
            package_id=cur.lastrowid,
        )
        return self.db.query_one(
            "SELECT * FROM packages WHERE id=?", (cur.lastrowid,)
        )

    def update_package(self, package_id: int, **fields: Any) -> None:
        fields.setdefault("updated_at", utcnow())
        keys = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE packages SET {keys} WHERE id=?", (*fields.values(), package_id)
        )

    def _reset_package(
        self, package: dict[str, Any], reason: str, *, keep_staging: bool = False
    ) -> None:
        package_id = package["id"]
        self.db.execute("DELETE FROM manifest_entries WHERE package_id=?", (package_id,))
        self.update_package(
            package_id,
            state="detected",
            failure_code=None,
            reason=reason,
            batch_id=None,
            manifest_sha256=None,
            zip_sha256=None,
            zip_bytes=0,
            member_count=0,
            total_uncompressed=0,
            progress_json=None,
        )
        staging = self.task_dir / f"pkg-{package_id}"
        if not keep_staging:
            remove_tree(staging)

    # -- receiving -------------------------------------------------------
    def _receive(self, package: dict[str, Any], folder: Path) -> None:
        package_id = package["id"]
        staging = self.task_dir / f"pkg-{package_id}"
        remove_tree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        self.update_package(
            package_id,
            state=STATE_RECEIVING,
            reason="正在复制清单与压缩包到本任务暂存目录。",
            failure_code=None,
        )
        self.add_event(
            "info", "receiving", "开始接收交付包，复制到任务暂存目录。", package_id=package_id
        )
        self.set_progress(
            package_counts=self.counts(),
            current_package={
                "id": package_id,
                "folder": folder.name,
                "stage": STATE_RECEIVING,
                "percent": 0,
            },
        )

        manifest_source = folder / MANIFEST_NAME
        if manifest_source.is_symlink():
            return self._fail_package(
                package_id, folder, "manifest_symlink", "manifest.json 不能是符号链接。"
            )
        if not manifest_source.is_file():
            return self._fail_package(
                package_id, folder, "manifest_missing", "缺少 manifest.json，无法接收交付包。"
            )

        manifest_dest = staging / MANIFEST_NAME
        try:
            manifest_bytes = manifest_source.read_bytes()
            manifest_dest.write_bytes(manifest_bytes)
        except OSError as exc:
            return self._fail_package(
                package_id, folder, "manifest_unreadable", f"复制 manifest.json 失败：{exc}"
            )
        manifest_hash = _digest_bytes(manifest_bytes)

        try:
            after = manifest_source.read_bytes()
        except OSError:
            after = b""
        if _digest_bytes(after) != manifest_hash:
            return self._fail_package(
                package_id,
                folder,
                "manifest_changed",
                "接收过程中 manifest.json 发生变化，请等待写入完成后重试。",
            )

        manifest, failure = load_manifest(
            manifest_dest, self.settings.max_manifest_bytes
        )
        if failure:
            return self._fail_package(
                package_id, folder, failure.code, failure.reason, failure.detail
            )

        zip_name = peek_zip_name(manifest_dest, self.settings.max_manifest_bytes)
        if zip_name is None:
            return self._fail_package(
                package_id,
                folder,
                "manifest_fields",
                "无法从 manifest.json 的 zip 字段确定压缩包名称。",
            )

        zip_source = folder / zip_name
        if zip_source.is_symlink():
            return self._fail_package(
                package_id, folder, "zip_symlink", "压缩包不能是符号链接。"
            )
        if not zip_source.is_file():
            return self._fail_package(
                package_id,
                folder,
                "zip_missing",
                f"清单指定的压缩包不存在：{zip_name}",
                {"path": zip_name},
            )

        zip_dest = staging / zip_name
        self.update_package(
            package_id,
            state=STATE_RECEIVING,
            reason="正在复制压缩包并重新计算散列。",
            zip_name=zip_name,
        )

        zip_hash, failure = self._copy_zip(
            package_id, zip_source, zip_dest
        )
        if failure:
            return self._fail_package(
                package_id, folder, failure.code, failure.reason, failure.detail
            )
        zip_bytes = zip_dest.stat().st_size

        if zip_hash != manifest.zip_sha256:
            self.update_package(
                package_id,
                zip_sha256=zip_hash,
                zip_bytes=zip_bytes,
                zip_name=zip_name,
                manifest_sha256=manifest_hash,
            )
            return self._fail_package(
                package_id,
                folder,
                "zip_hash_mismatch",
                "压缩包 SHA-256 与清单不一致，可能是文件尚未写完；正式数据未做任何改动。",
                {"expected": manifest.zip_sha256, "actual": zip_hash},
            )

        current_snapshot = read_snapshot(folder)
        if not same_sizes(self._stable.get(folder.name, {}), current_snapshot):
            return self._fail_package(
                package_id,
                folder,
                "source_changed",
                "接收期间交付目录仍发生变化，本轮放弃，等待文件稳定后重试。",
            )

        self.update_package(
            package_id,
            state=STATE_RECEIVED,
            reason="清单与压缩包已复制完成，开始核对。",
            manifest_sha256=manifest_hash,
            zip_sha256=zip_hash,
            zip_bytes=zip_bytes,
            package_code=manifest.package_code,
            zip_name=manifest.zip_name,
            failure_code=None,
            source_fingerprint=_fingerprint(current_snapshot),
        )
        self.add_event(
            "info",
            "received",
            f"已接收交付包 {manifest.package_code}，压缩包 {zip_bytes} 字节。",
            package_id=package_id,
        )
        self.set_progress(
            package_counts=self.counts(),
            current_package={
                "id": package_id,
                "folder": folder.name,
                "stage": STATE_RECEIVED,
                "percent": 100,
            },
        )
        self._process_staged(
            self.db.query_one("SELECT * FROM packages WHERE id=?", (package_id,))
        )

    def _copy_zip(
        self, package_id: int, source: Path, dest: Path
    ) -> tuple[str | None, Failure | None]:
        """Copy with coarse progress updates; hashing happens inside."""
        size = max(source.stat().st_size, 1)
        result: dict[str, Any] = {}

        def run() -> None:
            try:
                result["value"] = copy_zip_to_staging(source, dest)
            except Exception as exc:  # pragma: no cover - defensive
                result["value"] = (
                    None,
                    Failure("zip_unreadable", f"复制压缩包失败：{exc}"),
                )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        while thread.is_alive():
            copied = dest.stat().st_size if dest.exists() else 0
            self.set_progress(
                current_package={
                    "id": package_id,
                    "folder": source.parent.name,
                    "stage": STATE_RECEIVING,
                    "percent": min(99, int(copied * 100 / size)),
                }
            )
            thread.join(0.2)
        thread.join()
        return result.get("value", (None, Failure("zip_unreadable", "复制压缩包失败")))

    # -- validation / extraction / publish -------------------------------
    def _process_staged(self, package: dict[str, Any] | None) -> None:
        if package is None:
            return
        package_id = package["id"]
        staging = self.task_dir / f"pkg-{package_id}"
        folder = self.source_dir / package["relative_path"]

        if package["zip_sha256"] and self._batch_is_discarded(package):
            # The user resolved this content by keeping another batch (for
            # example the task restarted mid-conflict). Never publish it again.
            self.update_package(
                package_id,
                state="discarded",
                failure_code=None,
                reason="该内容已被用户丢弃，不再继续处理。",
                progress_json=None,
            )
            remove_tree(staging)
            self.set_progress(package_counts=self.counts(), current_package=None)
            return

        manifest, failure = load_manifest(
            staging / MANIFEST_NAME, self.settings.max_manifest_bytes
        )
        if failure:
            return self._fail_package(
                package_id, folder, failure.code, failure.reason, failure.detail
            )

        zip_name = manifest.zip_name
        zip_path = staging / zip_name
        if not zip_path.is_file():
            source_zip = folder / zip_name
            if source_zip.is_file() and not source_zip.is_symlink():
                _, failure = copy_zip_to_staging(source_zip, zip_path)
                if failure:
                    return self._fail_package(
                        package_id, folder, failure.code, failure.reason, failure.detail
                    )
            else:
                return self._fail_package(
                    package_id, folder, "zip_missing", "暂存目录缺少压缩包，且源文件不可用。"
                )

        zip_hash = sha256_file(zip_path)
        if zip_hash != manifest.zip_sha256:
            return self._fail_package(
                package_id,
                folder,
                "zip_hash_mismatch",
                "暂存压缩包 SHA-256 与清单不一致。",
                {"expected": manifest.zip_sha256, "actual": zip_hash},
            )

        self.update_package(
            package_id,
            state=STATE_VALIDATING,
            reason="正在检查压缩包路径、文件数量与展开大小。",
            package_code=manifest.package_code,
            zip_name=zip_name,
            zip_sha256=zip_hash,
            zip_bytes=zip_path.stat().st_size,
        )
        self.set_progress(
            package_counts=self.counts(),
            current_package={
                "id": package_id,
                "folder": folder.name,
                "stage": STATE_VALIDATING,
                "percent": 0,
            },
        )

        inspection, failure = inspect_zip(
            zip_path,
            max_files=self.settings.max_files_per_package,
            max_single_file_bytes=self.settings.max_single_file_bytes,
            max_total_expanded_bytes=self.settings.max_total_expanded_bytes,
        )
        if failure:
            self._record_inspection_failure(package_id, manifest, failure)
            return self._fail_package(
                package_id, folder, failure.code, failure.reason, failure.detail,
                keep_entries=True,
            )

        mismatch = compare_manifest(manifest, inspection)
        if mismatch:
            self._record_manifest_rows(package_id, manifest, inspection, mismatch)
            return self._fail_package(
                package_id, folder, mismatch.code, mismatch.reason, mismatch.detail,
                keep_entries=True,
            )

        self.update_package(
            package_id,
            member_count=inspection.member_count,
            total_uncompressed=inspection.total_uncompressed,
        )
        self.add_event(
            "info",
            "zip_inspected",
            f"压缩包检查通过：{inspection.member_count} 个文件，展开 {inspection.total_uncompressed} 字节。",
            package_id=package_id,
            detail={
                "members": inspection.member_count,
                "total_uncompressed": inspection.total_uncompressed,
            },
        )

        extract_root = staging / "extract"
        remove_tree(extract_root)
        extract_root.mkdir(parents=True)
        self.update_package(
            package_id,
            state=STATE_EXTRACTING,
            reason="正在解压并逐个校验文件。",
        )
        self.set_progress(
            current_package={
                "id": package_id,
                "folder": folder.name,
                "stage": STATE_EXTRACTING,
                "percent": 0,
            }
        )

        last = {"percent": -1}

        def progress(done: int, total: int, current: str) -> None:
            percent = min(99, int(done * 100 / max(total, 1)))
            if percent != last["percent"]:
                last["percent"] = percent
                self.set_progress(
                    current_package={
                        "id": package_id,
                        "folder": folder.name,
                        "stage": STATE_EXTRACTING,
                        "percent": percent,
                        "current_file": current,
                    }
                )

        extracted, failure = extract_and_hash(
            zip_path,
            manifest,
            extract_root,
            max_single_file_bytes=self.settings.max_single_file_bytes,
            max_total_expanded_bytes=self.settings.max_total_expanded_bytes,
            max_files=self.settings.max_files_per_package,
            progress=progress,
        )
        if failure:
            return self._fail_package(
                package_id, folder, failure.code, failure.reason, failure.detail
            )

        self.update_package(
            package_id, state=STATE_VERIFYING, reason="正在核对逐文件散列。"
        )
        results, failure = verify_extracted(manifest, extracted)
        self._store_manifest_rows(package_id, results)
        if failure:
            return self._fail_package(
                package_id, folder, failure.code, failure.reason,
                failure.detail, keep_entries=True,
            )

        self.add_event(
            "info",
            "verified",
            f"全部 {len(results)} 个文件校验通过。",
            package_id=package_id,
            detail={"files": len(results)},
        )

        existing_batch = None
        if package["state"] == STATE_PUBLISHING and package["batch_id"]:
            candidate = self.db.query_one(
                "SELECT * FROM batches WHERE id=?", (package["batch_id"],)
            )
            if candidate and candidate["state"] == "publishing":
                existing_batch = candidate

        linked = self._link_existing_batch(package, manifest, zip_path)
        if linked:
            return

        if existing_batch is not None:
            batch_id, conflict = existing_batch["id"], False
        else:
            batch_id, conflict = self._create_or_conflict_batch(
                package, manifest, zip_path, results
            )
        if conflict:
            self.update_package(
                package_id,
                state="conflict",
                failure_code="code_conflict",
                reason=(
                    f"编号 {manifest.package_code} 已存在内容不同的批次，"
                    "等待用户在冲突面板处理。"
                ),
                batch_id=batch_id,
                progress_json=None,
            )
            self.set_progress(package_counts=self.counts(), current_package=None)
            return

        self._publish(package, manifest, batch_id)

    def _batch_is_discarded(self, package: dict[str, Any]) -> bool:
        row = self.db.query_one(
            "SELECT state FROM batches WHERE task_id=? AND zip_sha256=?"
            " AND state='discarded'",
            (self.task_id, package["zip_sha256"]),
        )
        return row is not None

    # -- identity / batches ---------------------------------------------
    def _link_existing_batch(
        self, package: dict[str, Any], manifest: Manifest, zip_path: Path
    ) -> bool:
        package_id = package["id"]
        zip_hash = sha256_file(zip_path)
        existing = self.db.query_one(
            "SELECT * FROM batches WHERE task_id=? AND package_code=? AND zip_sha256=?"
            " AND state != 'discarded'",
            (self.task_id, manifest.package_code, zip_hash),
        )
        if existing is None:
            return False
        if package["state"] == STATE_PUBLISHING and package["batch_id"] == existing["id"]:
            return False
        if existing["state"] == "conflict":
            self.update_package(
                package_id,
                state="conflict",
                failure_code="code_conflict",
                reason=(
                    f"相同内容此前已进入冲突批次 #{existing['id']}，仍等待用户处理。"
                ),
                batch_id=existing["id"],
            )
            return True
        self.update_package(
            package_id,
            state="linked",
            failure_code=None,
            reason=f"内容与批次 #{existing['id']} 完全一致，已关联，不重复发布。",
            batch_id=existing["id"],
        )
        self.add_event(
            "info",
            "batch_linked",
            f"交付包与批次 #{existing['id']}（{manifest.package_code}）内容一致，已关联。",
            package_id=package_id,
            batch_id=existing["id"],
            detail={"sha256": zip_hash},
        )
        remove_tree(self.task_dir / f"pkg-{package_id}")
        self.set_progress(package_counts=self.counts(), current_package=None)
        return True

    def _create_or_conflict_batch(
        self,
        package: dict[str, Any],
        manifest: Manifest,
        zip_path: Path,
        results: list[dict[str, Any]],
    ) -> tuple[int, bool]:
        package_id = package["id"]
        zip_hash = sha256_file(zip_path)
        same_hash = self.db.query_one(
            "SELECT * FROM batches WHERE task_id=? AND package_code=?"
            " AND zip_sha256=? AND state != 'discarded'",
            (self.task_id, manifest.package_code, zip_hash),
        )
        if same_hash is not None:
            self.db.execute(
                "UPDATE batches SET state='publishing', source_package_id=?,"
                " updated_at=? WHERE id=?",
                (package_id, utcnow(), same_hash["id"]),
            )
            self.update_package(package_id, batch_id=same_hash["id"])
            self._replace_batch_files(same_hash["id"], results)
            return same_hash["id"], False

        existing_code = self.db.query_one(
            "SELECT * FROM batches WHERE task_id=? AND package_code=?"
            " AND state IN ('published','publishing','conflict')"
            " ORDER BY version DESC",
            (self.task_id, manifest.package_code),
        )
        if existing_code is not None:
            batch_id = self._insert_batch(
                manifest,
                package_id,
                "conflict",
                results,
                version=existing_code["version"] + 1,
            )
            self._store_batch_files(batch_id, results, self.task_dir / f"pkg-{package_id}" / "extract", manifest)
            self.add_event(
                "warn",
                "code_conflict",
                f"交付编号 {manifest.package_code} 与批次 #{existing_code['id']} 内容不同，等待用户决定。",
                package_id=package_id,
                batch_id=batch_id,
                detail={
                    "existing_batch": existing_code["id"],
                    "existing_sha256": existing_code["zip_sha256"],
                    "incoming_sha256": zip_hash,
                },
            )
            return batch_id, True

        batch_id = self._insert_batch(manifest, package_id, "publishing", results, version=1)
        self._store_batch_files(batch_id, results, self.task_dir / f"pkg-{package_id}" / "extract", manifest)
        self.add_event(
            "info",
            "batch_created",
            f"创建批次 #{batch_id}（{manifest.package_code}）。",
            package_id=package_id,
            batch_id=batch_id,
            detail={"files": len(results)},
        )
        return batch_id, False

    def _replace_batch_files(
        self, batch_id: int, results: list[dict[str, Any]]
    ) -> None:
        self.db.execute("DELETE FROM batch_files WHERE batch_id=?", (batch_id,))
        self._store_batch_files(
            batch_id,
            results,
            self.task_dir / f"batch-{batch_id}",
            Manifest("", "", "", []),
        )

    def _insert_batch(
        self,
        manifest: Manifest,
        package_id: int,
        state: str,
        results: list[dict[str, Any]],
        *,
        version: int,
    ) -> int:
        now = utcnow()
        total_bytes = sum((r["actual_bytes"] or 0) for r in results)
        cur = self.db.execute(
            "INSERT INTO batches (task_id, package_code, zip_sha256, zip_name, state,"
            " version, source_package_id, file_count, total_bytes, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.task_id,
                manifest.package_code,
                manifest.zip_sha256,
                manifest.zip_name,
                state,
                version,
                package_id,
                len(results),
                total_bytes,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def _store_batch_files(
        self,
        batch_id: int,
        results: list[dict[str, Any]],
        root: Path,
        manifest: Manifest | None = None,
    ) -> None:
        rows = []
        for row in results:
            rel = row["rel_path"]
            full = root / Path(rel)
            rows.append(
                (
                    batch_id,
                    rel,
                    str(full),
                    row["actual_bytes"] or 0,
                    row["actual_sha256"] or "",
                    row["result"],
                    row["reason"],
                    row["sort_order"],
                )
            )
        self.db.executemany(
            "INSERT INTO batch_files (batch_id, rel_path, full_path, byte_size, sha256,"
            " result, reason, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    # -- publishing ------------------------------------------------------
    def _publish(
        self, package: dict[str, Any], manifest: Manifest, batch_id: int
    ) -> None:
        package_id = package["id"]
        staging = self.task_dir / f"pkg-{package_id}"
        extract_root = staging / "extract"
        target = self.settings.publish_dir / manifest.package_code
        build = self.task_dir / f"batch-{batch_id}"

        self.update_package(
            package_id,
            state=STATE_PUBLISHING,
            reason="校验通过，正在组装发布目录。",
            batch_id=batch_id,
        )
        self.set_progress(
            package_counts=self.counts(),
            current_package={
                "id": package_id,
                "folder": self.source_dir.name,
                "stage": STATE_PUBLISHING,
                "percent": 0,
            },
        )

        remove_tree(build)
        try:
            build.mkdir(parents=True)
            if extract_root.is_dir():
                for dirpath, dirnames, filenames in os.walk(extract_root, followlinks=False):
                    rel = Path(dirpath).relative_to(extract_root)
                    dest_dir = build / rel
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    for d in dirnames:
                        if (Path(dirpath) / d).is_symlink():
                            raise RuntimeError(f"发布暂存出现符号链接：{Path(dirpath) / d}")
                    for name in filenames:
                        src = Path(dirpath) / name
                        dst = dest_dir / name
                        shutil.copy2(src, dst)
                        if dst.is_symlink():
                            raise RuntimeError(f"发布暂存出现符号链接：{dst}")
            shutil.copy2(staging / MANIFEST_NAME, build / MANIFEST_NAME)
            shutil.copy2(staging / manifest.zip_name, build / manifest.zip_name)
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"发布目标已存在：{target}")
            atomic_publish(build, target)
        except Exception as exc:
            log.exception("publish failed for batch %s", batch_id)
            remove_tree(build)
            self.db.execute(
                "UPDATE batches SET state='failed', publish_reason=?, updated_at=? WHERE id=?",
                (str(exc), utcnow(), batch_id),
            )
            return self._fail_package(
                package_id,
                self.source_dir / package["relative_path"],
                "publish_error",
                f"发布失败：{exc}（正式数据未受影响）",
            )

        now = utcnow()
        self.db.execute(
            "UPDATE batches SET state='published', publish_path=?, updated_at=? WHERE id=?",
            (str(target), now, batch_id),
        )
        # Batch file paths always point at the final published location, so
        # previews and downloads work after staging is cleaned up.
        self.db.executemany(
            "UPDATE batch_files SET full_path=? WHERE batch_id=? AND rel_path=?",
            [
                (str(target / Path(row["rel_path"])), batch_id, row["rel_path"])
                for row in self.db.query(
                    "SELECT rel_path FROM batch_files WHERE batch_id=?", (batch_id,)
                )
            ],
        )
        self.update_package(
            package_id,
            state="published",
            failure_code=None,
            reason=f"已发布到 {target}",
            batch_id=batch_id,
            progress_json=None,
        )
        self.add_event(
            "info",
            "published",
            f"批次 #{batch_id}（{manifest.package_code}）已发布到 {target}。",
            package_id=package_id,
            batch_id=batch_id,
            detail={"target": str(target)},
        )
        remove_tree(staging)
        self.set_progress(package_counts=self.counts(), current_package=None)

    # -- failure helpers --------------------------------------------------
    def _fail_package(
        self,
        package_id: int,
        folder: Path,
        code: str,
        reason: str,
        detail: dict[str, Any] | None = None,
        *,
        keep_entries: bool = False,
    ) -> None:
        if not keep_entries:
            self.db.execute(
                "DELETE FROM manifest_entries WHERE package_id=?", (package_id,)
            )
        snapshot = read_snapshot(folder) if folder.exists() else {}
        self.update_package(
            package_id,
            state="failed",
            failure_code=code,
            reason=reason,
            objects_json=json.dumps(object_view(folder), ensure_ascii=False),
            source_fingerprint=_fingerprint(snapshot),
            progress_json=None,
        )
        self.add_event("error", code, reason, package_id=package_id, detail=detail)
        remove_tree(self.task_dir / f"pkg-{package_id}")
        self.set_progress(package_counts=self.counts(), current_package=None)

    def _record_inspection_failure(
        self, package_id: int, manifest: Manifest, failure: Failure
    ) -> None:
        rows = [
            {
                "rel_path": e.rel_path,
                "expected_bytes": e.byte_size,
                "expected_sha256": e.sha256,
                "actual_bytes": None,
                "actual_sha256": None,
                "result": "not_checked",
                "reason": f"压缩包检查未通过（{failure.code}），未解压",
                "sort_order": e.sort_order,
            }
            for e in manifest.entries
        ]
        self._store_manifest_rows(package_id, rows)

    def _record_manifest_rows(
        self,
        package_id: int,
        manifest: Manifest,
        inspection: Any,
        failure: Failure,
    ) -> None:
        actual = {m.name: m for m in inspection.members}
        rows: list[dict[str, Any]] = []
        for e in manifest.entries:
            member = actual.get(e.rel_path)
            if member is None:
                result, reason = "missing", "压缩包内缺少该文件"
            elif member.byte_size != e.byte_size:
                result, reason = (
                    "size_mismatch",
                    f"清单 {e.byte_size} 字节，压缩包内 {member.byte_size} 字节",
                )
            else:
                result, reason = "not_checked", "内容核对未完成（清单整体不符）"
            rows.append(
                {
                    "rel_path": e.rel_path,
                    "expected_bytes": e.byte_size,
                    "expected_sha256": e.sha256,
                    "actual_bytes": member.byte_size if member else None,
                    "actual_sha256": None,
                    "result": result,
                    "reason": reason,
                    "sort_order": e.sort_order,
                }
            )
        extra = sorted(set(actual) - {e.rel_path for e in manifest.entries})
        for index, name in enumerate(extra, start=len(rows)):
            rows.append(
                {
                    "rel_path": name,
                    "expected_bytes": 0,
                    "expected_sha256": "",
                    "actual_bytes": actual[name].byte_size,
                    "actual_sha256": None,
                    "result": "extra",
                    "reason": "压缩包内多出该文件，清单未登记",
                    "sort_order": index,
                }
            )
        self._store_manifest_rows(package_id, rows)

    def _store_manifest_rows(
        self, package_id: int, rows: list[dict[str, Any]]
    ) -> None:
        self.db.execute("DELETE FROM manifest_entries WHERE package_id=?", (package_id,))
        if not rows:
            return
        self.db.executemany(
            "INSERT INTO manifest_entries (package_id, rel_path, expected_bytes,"
            " expected_sha256, actual_bytes, actual_sha256, result, reason, sort_order)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    package_id,
                    r["rel_path"],
                    r["expected_bytes"],
                    r["expected_sha256"],
                    r["actual_bytes"],
                    r["actual_sha256"],
                    r["result"],
                    r["reason"],
                    r["sort_order"],
                )
                for r in rows
            ],
        )


def object_view(folder: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name)
    except OSError:
        return items
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
            if entry.is_symlink():
                kind = "symlink"
            elif entry.is_dir(follow_symlinks=False):
                kind = "dir"
            else:
                kind = "file"
            items.append(
                {
                    "name": entry.name,
                    "kind": kind,
                    "bytes": st.st_size,
                    "mtime": st.st_mtime_ns,
                }
            )
        except OSError:
            continue
    return items





class WorkerRegistry:
    """Owns one worker thread per running task."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._workers: dict[int, TaskWorker] = {}
        self._lock = threading.Lock()

    def start(self, task_id: int) -> TaskWorker:
        with self._lock:
            worker = self._workers.get(task_id)
            if worker is not None and worker.is_alive():
                worker.wake()
                return worker
            worker = TaskWorker(task_id, self.settings, self.db)
            self._workers[task_id] = worker
            worker.start()
            return worker

    def stop(self, task_id: int, *, wait: bool = True) -> None:
        with self._lock:
            worker = self._workers.get(task_id)
        if worker is None:
            return
        worker.stop()
        if wait:
            worker.join(timeout=10)
        with self._lock:
            if not worker.is_alive():
                self._workers.pop(task_id, None)

    def wake(self, task_id: int) -> None:
        with self._lock:
            worker = self._workers.get(task_id)
        if worker is not None:
            worker.wake()

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=10)

    def get(self, task_id: int) -> TaskWorker | None:
        with self._lock:
            return self._workers.get(task_id)

    def is_running(self, task_id: int) -> bool:
        with self._lock:
            worker = self._workers.get(task_id)
        return bool(worker and worker.is_alive())
