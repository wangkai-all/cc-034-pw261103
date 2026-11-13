"""End-to-end worker tests: normal flow, stability, dedupe, conflict, failures."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.delivery import remove_tree, sha256_file
from app.worker import TaskWorker
from tests.support import (
    SAMPLE_FILES,
    make_manifest,
    make_package,
    sha256,
    write_conflict_package,
    write_zip,
)


def scan(worker: TaskWorker) -> None:
    worker.scan_round(force=True)


def package_row(worker: TaskWorker, key: str) -> dict:
    row = worker.db.query_one(
        "SELECT * FROM packages WHERE task_id=? AND package_key=?",
        (worker.task_id, key),
    )
    assert row is not None, f"package {key} not tracked"
    return row


def settle(worker: TaskWorker, directory: Path, rounds: int = 3) -> None:
    """Scan until the package reaches a terminal state or rounds run out."""
    for _ in range(rounds):
        scan(worker)
        row = package_row(worker, directory.name)
        if row["state"] in {"published", "linked", "failed", "conflict", "discarded"}:
            return
    scan(worker)


def test_normal_package_publishes_atomically(
    settings, source: Path, worker_factory
) -> None:
    folder = make_package(source / "pkg-a", "PKG-A")
    worker = worker_factory(source)

    scan(worker)  # first sight: sizes recorded, not yet stable
    row = package_row(worker, "pkg-a")
    assert row["state"] == "waiting_stable"

    scan(worker)  # second identical scan: stable -> receive -> publish
    row = package_row(worker, "pkg-a")
    assert row["state"] == "published", row["reason"]

    published = settings.publish_dir / "PKG-A"
    assert published.is_dir()
    assert (published / "manifest.json").read_bytes() == (
        folder / "manifest.json"
    ).read_bytes()
    assert (published / "PKG-A.zip").is_file()
    for name, data in SAMPLE_FILES.items():
        assert (published / name).read_bytes() == data

    batch = worker.db.query_one(
        "SELECT * FROM batches WHERE task_id=?", (worker.task_id,)
    )
    assert batch["state"] == "published"
    files = worker.db.query(
        "SELECT * FROM batch_files WHERE batch_id=?", (batch["id"],)
    )
    assert len(files) == len(SAMPLE_FILES)
    assert all(f["result"] == "ok" for f in files)
    assert all((Path(f["full_path"]).read_bytes() == SAMPLE_FILES[f["rel_path"]])
               for f in files)

    # Staging was cleaned up after publishing.
    assert not (settings.staging_dir / f"task-{worker.task_id}" / "pkg-1").exists()
    assert not list(settings.staging_dir.rglob("*.tmp"))


def test_growing_file_is_waited_for(settings, source: Path, worker_factory) -> None:
    folder = source / "growing"
    folder.mkdir()
    zip_path = folder / "PKG-G.zip"
    write_zip(zip_path, SAMPLE_FILES)
    manifest = make_manifest(
        "PKG-G", "PKG-G.zip", SAMPLE_FILES, zip_sha256=sha256_file(zip_path)
    )
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    worker = worker_factory(source)
    scan(worker)
    scan(worker)
    assert package_row(worker, "growing")["state"] == "published"

    # A second package appears while its ZIP is only half written.
    folder2 = source / "growing-2"
    folder2.mkdir()
    zip_path2 = folder2 / "PKG-G2.zip"
    write_zip(zip_path2, SAMPLE_FILES)
    bulk = zip_path2.read_bytes()
    zip_path2.write_bytes(bulk[: len(bulk) // 2])
    (folder2 / "manifest.json").write_text(
        json.dumps(
            make_manifest(
                "PKG-G2", "PKG-G2.zip", SAMPLE_FILES,
                zip_sha256=sha256(bulk),
            )
        ),
        encoding="utf-8",
    )
    scan(worker)
    assert package_row(worker, "growing-2")["state"] == "waiting_stable"

    zip_path2.write_bytes(bulk)
    scan(worker)
    scan(worker)
    row = package_row(worker, "growing-2")
    assert row["state"] == "published", row["reason"]


def test_unchanged_size_with_bad_hash_fails_without_publishing(
    settings, source: Path, worker_factory
) -> None:
    folder = make_package(
        source / "bad-hash", "PKG-B",
        manifest=make_manifest(
            "PKG-B", "PKG-B.zip", SAMPLE_FILES, zip_sha256="a" * 64
        ),
    )
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "bad-hash")
    assert row["state"] == "failed"
    assert row["failure_code"] == "zip_hash_mismatch"
    assert not (settings.publish_dir / "PKG-B").exists()
    # Pairing is recorded so it can be inspected later.
    assert row["zip_sha256"] is not None
    detail = worker.db.query(
        "SELECT * FROM events WHERE package_id=? AND code='zip_hash_mismatch'",
        (row["id"],),
    )
    assert detail and json.loads(detail[0]["detail_json"])["expected"] == "a" * 64


def test_missing_zip_reports_reason(settings, source: Path, worker_factory) -> None:
    folder = source / "no-zip"
    folder.mkdir()
    (folder / "manifest.json").write_text(
        json.dumps(make_manifest("PKG-N", "PKG-N.zip", SAMPLE_FILES)),
        encoding="utf-8",
    )
    worker = worker_factory(source)
    scan(worker)
    row = package_row(worker, "no-zip")
    assert row["state"] == "waiting_stable"
    scan(worker)
    row = package_row(worker, "no-zip")
    assert row["state"] == "failed"
    assert row["failure_code"] == "zip_missing"
    assert "PKG-N.zip" in row["reason"]


def test_partial_manifest_reports_reason(settings, source: Path, worker_factory) -> None:
    folder = make_package(source / "partial", "PKG-P")
    worker = worker_factory(source)
    settle(worker, folder)
    batch = worker.db.query_one(
        "SELECT * FROM batches WHERE task_id=?", (worker.task_id,)
    )
    assert batch is not None

    # An identical package (same code, same bytes) only links to the batch.
    duplicate = make_package(source / "same-content", "PKG-P")
    settle(worker, duplicate)
    row = package_row(worker, "same-content")
    assert row["state"] == "linked"
    assert row["batch_id"] == batch["id"]
    batches = worker.db.query(
        "SELECT * FROM batches WHERE task_id=?", (worker.task_id,)
    )
    assert len(batches) == 1


def test_same_code_different_content_becomes_conflict(
    settings, source: Path, worker_factory
) -> None:
    first = make_package(source / "conflict-1", "PKG-C")
    worker = worker_factory(source)
    settle(worker, first)
    assert package_row(worker, "conflict-1")["state"] == "published"

    second = write_conflict_package(source / "conflict-2", "PKG-C")
    settle(worker, second)
    row = package_row(worker, "conflict-2")
    assert row["state"] == "conflict"
    assert row["failure_code"] == "code_conflict"
    assert row["batch_id"] is not None

    published = settings.publish_dir / "PKG-C"
    assert (published / "README.txt").read_bytes() == SAMPLE_FILES["README.txt"]
    batches = worker.db.query(
        "SELECT * FROM batches WHERE task_id=? ORDER BY id", (worker.task_id,)
    )
    assert len(batches) == 2
    assert batches[0]["state"] == "published"
    assert batches[1]["state"] in {"conflict", "publishing"}
    assert batches[1]["zip_sha256"] != batches[0]["zip_sha256"]


def test_path_traversal_zip_fails_before_extraction(
    settings, source: Path, worker_factory
) -> None:
    folder = source / "evil"
    folder.mkdir()
    zip_path = folder / "PKG-E.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data in SAMPLE_FILES.items():
            zf.writestr(name, data)
        zf.writestr("../escape.txt", b"nope")
    (folder / "manifest.json").write_text(
        json.dumps(
            make_manifest("PKG-E", "PKG-E.zip", SAMPLE_FILES,
                          zip_sha256=sha256_file(zip_path))
        ),
        encoding="utf-8",
    )
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "evil")
    assert row["state"] == "failed"
    assert row["failure_code"] == "path_invalid"
    assert not (settings.publish_dir / "PKG-E").exists()
    assert not (settings.staging_dir / f"task-{worker.task_id}" / f"pkg-{row['id']}").exists()


def test_expansion_limit_enforced_from_central_directory(
    settings, source: Path, worker_factory
) -> None:
    files = {"data/part-a.log": b"x" * (30 * 1024 * 1024)}
    files["data/part-b.log"] = b"y" * (30 * 1024 * 1024)
    folder = make_package(source / "over-limit", "PKG-O", files)
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "over-limit")
    assert row["state"] == "failed"
    assert row["failure_code"] == "expanded_too_large"
    # Manifest entries are still recorded for inspection.
    entries = worker.db.query(
        "SELECT * FROM manifest_entries WHERE package_id=?", (row["id"],)
    )
    assert len(entries) == len(files)
    assert all(e["result"] == "not_checked" for e in entries)


def test_too_many_files_is_rejected(
    settings, source: Path, worker_factory
) -> None:
    files = {f"part-{i}.txt": b"x" for i in range(210)}
    folder = make_package(source / "many", "PKG-M", files)
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "many")
    assert row["state"] == "failed"
    assert row["failure_code"] == "too_many_files"


def test_manifest_mismatch_lists_missing_and_extra(
    settings, source: Path, worker_factory
) -> None:
    zip_files = {k: v for k, v in SAMPLE_FILES.items() if k != "README.txt"}
    zip_files["extra.txt"] = b"surprise\n"
    folder = source / "mismatch"
    folder.mkdir()
    write_zip(folder / "PKG-X.zip", zip_files)
    manifest = make_manifest(
        "PKG-X", "PKG-X.zip", dict(SAMPLE_FILES),
        zip_sha256=sha256_file(folder / "PKG-X.zip"),
        extra_entries=[
            {"path": "docs/never-shipped.txt", "bytes": 4, "sha256": sha256(b"nope")}
        ],
    )
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "mismatch")
    assert row["state"] == "failed"
    assert row["failure_code"] == "manifest_mismatch"
    entries = {
        e["rel_path"]: e for e in worker.db.query(
            "SELECT * FROM manifest_entries WHERE package_id=?", (row["id"],)
        )
    }
    assert entries["README.txt"]["result"] == "missing"
    assert entries["docs/never-shipped.txt"]["result"] == "missing"
    assert entries["extra.txt"]["result"] == "extra"


def test_case_collision_inside_zip_is_rejected(
    settings, source: Path, worker_factory
) -> None:
    folder = source / "case"
    folder.mkdir()
    zip_path = folder / "PKG-K.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Report.txt", b"a")
        zf.writestr("report.txt", b"b")
    (folder / "manifest.json").write_text(
        json.dumps(
            make_manifest("PKG-K", "PKG-K.zip", {"Report.txt": b"a"},
                          zip_sha256=sha256_file(zip_path))
        ),
        encoding="utf-8",
    )
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "case")
    assert row["state"] == "failed"
    assert row["failure_code"] == "case_collision"


def test_per_file_hash_mismatch_fails(settings, source: Path, worker_factory) -> None:
    zip_files = dict(SAMPLE_FILES)
    folder = source / "hash"
    folder.mkdir()
    write_zip(folder / "PKG-H.zip", zip_files)
    fixed = make_manifest(
        "PKG-H", "PKG-H.zip", SAMPLE_FILES,
        zip_sha256=sha256_file(folder / "PKG-H.zip"),
        override_paths={"README.txt": {"sha256": "b" * 64}},
    )
    (folder / "manifest.json").write_text(json.dumps(fixed), encoding="utf-8")
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "hash")
    assert row["state"] == "failed"
    assert row["failure_code"] == "content_mismatch"
    entries = worker.db.query(
        "SELECT * FROM manifest_entries WHERE package_id=?", (row["id"],)
    )
    assert entries
    assert any(e["result"] == "hash_mismatch" for e in entries)
    assert not (settings.publish_dir / "PKG-H").exists()


def test_failed_package_is_not_republished_without_change(
    settings, source: Path, worker_factory
) -> None:
    folder = make_package(
        source / "fail-once", "PKG-F",
        manifest=make_manifest("PKG-F", "PKG-F.zip", SAMPLE_FILES, zip_sha256="c" * 64),
    )
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "fail-once")
    assert row["state"] == "failed"
    scan(worker)
    scan(worker)
    assert package_row(worker, "fail-once")["state"] == "failed"


def test_publish_target_already_exists_fails_cleanly(
    settings, source: Path, worker_factory
) -> None:
    settings.publish_dir.mkdir(parents=True, exist_ok=True)
    (settings.publish_dir / "PKG-T").mkdir()
    (settings.publish_dir / "PKG-T" / "stale.txt").write_text("old", encoding="utf-8")
    folder = make_package(source / "target", "PKG-T")
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "target")
    assert row["state"] == "failed"
    assert row["failure_code"] == "publish_error"
    assert (settings.publish_dir / "PKG-T" / "stale.txt").read_text(encoding="utf-8") == "old"
    assert not (settings.publish_dir / "PKG-T" / "README.txt").exists()


def test_symlink_in_source_is_never_followed(
    settings, source: Path, worker_factory
) -> None:
    folder = make_package(source / "link", "PKG-L")
    real = folder / "PKG-L.zip"
    link = folder / "PKG-L-link.zip"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    (folder / "manifest.json").write_text(
        json.dumps(make_manifest("PKG-L", "PKG-L-link.zip", SAMPLE_FILES)),
        encoding="utf-8",
    )
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "link")
    assert row["state"] == "failed"
    assert row["failure_code"] == "zip_symlink"


def test_scan_error_is_reported(settings, tmp_path: Path, worker_factory) -> None:
    missing = tmp_path / "gone"
    worker = worker_factory(missing)
    scan(worker)
    task = worker.db.query_one("SELECT * FROM tasks WHERE id=?", (worker.task_id,))
    assert task["scan_error"] == "源目录不存在"


def test_resume_after_interrupted_processing(
    settings, source: Path, worker_factory
) -> None:
    folder = make_package(source / "resume", "PKG-A2")
    worker = worker_factory(source)
    settle(worker, folder)
    row = package_row(worker, "resume")
    assert row["state"] == "published"

    # Simulate a crash after the ZIP was copied but before publishing: the
    # batch row and the publish directory were never created.
    batch_id = row["batch_id"]
    worker.db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    remove_tree(settings.publish_dir / "PKG-A2")
    worker.db.execute(
        "UPDATE packages SET state='received', batch_id=NULL WHERE id=?", (row["id"],)
    )
    scan(worker)
    row = package_row(worker, "resume")
    assert row["state"] == "published", row["reason"]
    assert (settings.publish_dir / "PKG-A2" / "README.txt").is_file()
    assert row["batch_id"] != batch_id
