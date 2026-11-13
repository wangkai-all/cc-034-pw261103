"""API tests: task creation, progress, package detail, preview/download, conflicts."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from tests.support import (
    SAMPLE_FILES,
    make_manifest,
    make_package,
    sha256_file,
    write_conflict_package,
    write_zip,
)


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    app = create_app(settings, Database(settings.db_path))
    with TestClient(app) as test_client:
        yield test_client


def wait_for(predicate, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting for condition")


def task_detail(client: TestClient, task_id: int) -> dict:
    return client.get(f"/api/tasks/{task_id}").json()


def packages(client: TestClient, task_id: int) -> list[dict]:
    return client.get(f"/api/tasks/{task_id}/packages").json()


def test_create_task_rejects_bad_paths(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/api/tasks", json={"directory": "relative/path"})
    assert response.status_code == 400
    assert "绝对路径" in response.json()["detail"]

    response = client.post("/api/tasks", json={"directory": str(tmp_path / "nope")})
    assert response.status_code == 400

    response = client.post("/api/tasks", json={"directory": "/"})
    assert response.status_code == 400
    assert "根目录" in response.json()["detail"]
    response = client.post(
        "/api/tasks",
        json={"directory": str(tmp_path), "allow_broad_directory": True},
    )
    assert response.status_code == 201
    assert response.json()["source_dir"] == str(tmp_path.resolve())


def test_create_task_rejects_data_directories(
    client: TestClient, settings: Settings
) -> None:
    response = client.post(
        "/api/tasks", json={"directory": str(settings.publish_dir)}
    )
    assert response.status_code == 400
    assert "本应用" in response.json()["detail"]


def test_full_flow_over_http(client: TestClient, settings: Settings, source: Path) -> None:
    make_package(source / "pkg-a", "PKG-A")
    created = client.post("/api/tasks", json={"directory": str(source)})
    assert created.status_code == 201
    task_id = created.json()["id"]

    wait_for(
        lambda: [
            p for p in packages(client, task_id) if p["state"] == "published"
        ]
        or None
    )

    detail = task_detail(client, task_id)
    assert detail["package_counts"].get("published") == 1
    assert detail["progress"]["scanning"]["status"] in {"ok", "scanning"}

    pkg = packages(client, task_id)[0]
    detail_pkg = client.get(f"/api/packages/{pkg['id']}").json()
    assert detail_pkg["checks"]["total"] == len(SAMPLE_FILES)
    assert all(e["result"] == "ok" for e in detail_pkg["entries"])
    batch = detail_pkg["batch"]
    assert batch["state"] == "published"

    batch_detail = client.get(f"/api/batches/{batch['id']}").json()
    assert len(batch_detail["files"]) == len(SAMPLE_FILES)

    for file in batch_detail["files"]:
        preview = client.get(
            f"/api/batches/{batch['id']}/files/{file['id']}/preview"
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        if file["rel_path"].endswith((".csv", ".json", ".txt")):
            assert body["kind"] == "text"
            assert body["content"]
        download = client.get(
            f"/api/batches/{batch['id']}/files/{file['id']}/download"
        )
        assert download.status_code == 200
        assert download.content == SAMPLE_FILES[file["rel_path"]]

    zip_download = client.get(f"/api/batches/{batch['id']}/zip")
    assert zip_download.status_code == 200
    assert zip_download.content[:2] == b"PK"

    events = client.get(f"/api/tasks/{task_id}/events").json()
    codes = {e["code"] for e in events}
    assert "published" in codes
    assert "received" in codes


def test_progress_endpoint_reports_scan_and_package_state(
    client: TestClient, settings: Settings, source: Path
) -> None:
    make_package(source / "pkg-a", "PKG-A")
    task = client.post("/api/tasks", json={"directory": str(source)}).json()
    detail = task_detail(client, task["id"])
    assert detail["limits"]["max_files"] == settings.max_files_per_package
    assert detail["publish_dir"] == str(settings.publish_dir)
    assert detail["status"] == "running"
    assert detail["progress"]["scanning"]["status"] in {
        "starting",
        "scanning",
        "ok",
    }

    paused = client.post(f"/api/tasks/{task['id']}/pause").json()
    assert paused["status"] == "paused"
    resumed = client.post(f"/api/tasks/{task['id']}/resume").json()
    assert resumed["status"] == "running"


def test_failure_detail_visible_and_publish_untouched(
    client: TestClient, settings: Settings, source: Path
) -> None:
    folder = source / "bad"
    folder.mkdir()
    write_zip(folder / "PKG-B.zip", SAMPLE_FILES)
    manifest = make_manifest(
        "PKG-B", "PKG-B.zip", SAMPLE_FILES, zip_sha256="f" * 64
    )
    (folder / "manifest.json").write_text(
        __import__("json").dumps(manifest), encoding="utf-8"
    )
    task = client.post("/api/tasks", json={"directory": str(source)}).json()
    wait_for(
        lambda: [
            p for p in packages(client, task["id"]) if p["state"] == "failed"
        ]
        or None
    )
    pkg = packages(client, task["id"])[0]
    detail = client.get(f"/api/packages/{pkg['id']}").json()
    assert detail["failure_code"] == "zip_hash_mismatch"
    assert "SHA-256" in detail["reason"]
    assert not (settings.publish_dir / "PKG-B").exists()
    events = client.get(f"/api/tasks/{task['id']}/events").json()
    assert any(e["code"] == "zip_hash_mismatch" for e in events)


def test_conflict_surface_and_resolution_over_http(
    client: TestClient, settings: Settings, source: Path
) -> None:
    make_package(source / "c1", "PKG-C")
    task = client.post("/api/tasks", json={"directory": str(source)}).json()
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "published"]
        or None
    )

    write_conflict_package(source / "c2", "PKG-C")
    client.post(f"/api/tasks/{task['id']}/rescan")
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "conflict"]
        or None
    )

    conflicts = client.get(f"/api/tasks/{task['id']}/conflicts").json()
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["existing_batch"]["id"] != conflict["incoming_batch"]["id"]
    assert (
        conflict["existing_batch"]["zip_sha256"]
        != conflict["incoming_batch"]["zip_sha256"]
    )

    resolved = client.post(
        f"/api/packages/{conflict['id']}/resolve",
        json={"resolution": "keep_existing"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolution"] == "keep_existing"
    assert (
        settings.publish_dir / "PKG-C" / "README.txt"
    ).read_bytes() == SAMPLE_FILES["README.txt"]

    conflicts_after = client.get(f"/api/tasks/{task['id']}/conflicts").json()
    assert conflicts_after == []


def test_conflict_receive_incoming_replaces_published_byte(
    client: TestClient, settings: Settings, source: Path
) -> None:
    make_package(source / "c1", "PKG-R")
    task = client.post("/api/tasks", json={"directory": str(source)}).json()
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "published"]
        or None
    )
    write_conflict_package(source / "c2", "PKG-R")
    client.post(f"/api/tasks/{task['id']}/rescan")
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "conflict"]
        or None
    )
    conflict = client.get(f"/api/tasks/{task['id']}/conflicts").json()[0]
    response = client.post(
        f"/api/packages/{conflict['id']}/resolve",
        json={"resolution": "receive_incoming"},
    )
    assert response.status_code == 200
    published = settings.publish_dir / "PKG-R"
    assert (published / "README.txt").read_bytes() == (
        b"a different payload with the same code\n"
    )
    assert (published / "PKG-R.zip").is_file()
    # The old batch is superseded, the new one published.
    batches = client.get(f"/api/tasks/{task['id']}/batches").json()
    states = {b["state"] for b in batches}
    assert "published" in states
    assert "superseded" in states


def test_preview_rejects_binary_and_unverified(
    client: TestClient, settings: Settings, source: Path
) -> None:
    folder = source / "pkg"
    folder.mkdir()
    files = dict(SAMPLE_FILES)
    files["blob.bin"] = b"\x00\x01\x02binary"
    write_zip(folder / "PKG-Z.zip", files)
    (folder / "manifest.json").write_text(
        __import__("json").dumps(
            make_manifest(
                "PKG-Z", "PKG-Z.zip", files,
                zip_sha256=sha256_file(folder / "PKG-Z.zip"),
            )
        ),
        encoding="utf-8",
    )
    task = client.post("/api/tasks", json={"directory": str(source)}).json()
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "published"]
        or None
    )
    pkg = packages(client, task["id"])[0]
    batch = client.get(f"/api/packages/{pkg['id']}").json()["batch"]
    detail = client.get(f"/api/batches/{batch['id']}").json()
    blob = next(f for f in detail["files"] if f["rel_path"].endswith(".bin"))
    preview = client.get(f"/api/batches/{batch['id']}/files/{blob['id']}/preview")
    assert preview.status_code == 200
    assert preview.json()["kind"] == "binary"
    download = client.get(
        f"/api/batches/{batch['id']}/files/{blob['id']}/download"
    )
    assert download.status_code == 200
    assert download.content == b"\x00\x01\x02binary"


def test_duplicate_content_links_to_existing_batch(
    client: TestClient, settings: Settings, source: Path
) -> None:
    make_package(source / "one", "PKG-D")
    task = client.post("/api/tasks", json={"directory": str(source)}).json()
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "published"]
        or None
    )
    make_package(source / "two", "PKG-D")
    client.post(f"/api/tasks/{task['id']}/rescan")
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "linked"]
        or None
    )
    rows = packages(client, task["id"])
    linked = next(p for p in rows if p["state"] == "linked")
    published = next(p for p in rows if p["state"] == "published")
    assert linked["batch_id"] == published["batch_id"]


def test_path_traversal_shown_in_package_detail(
    client: TestClient, settings: Settings, source: Path
) -> None:
    folder = source / "evil"
    folder.mkdir()
    zip_path = folder / "PKG-E.zip"
    write_zip(zip_path, SAMPLE_FILES)
    with __import__("zipfile").ZipFile(zip_path, "a") as zf:
        zf.writestr("../escape.txt", b"nope")
    (folder / "manifest.json").write_text(
        __import__("json").dumps(
            make_manifest(
                "PKG-E", "PKG-E.zip", SAMPLE_FILES,
                zip_sha256=sha256_file(zip_path),
            )
        ),
        encoding="utf-8",
    )
    task = client.post("/api/tasks", json={"directory": str(source)}).json()
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "failed"]
        or None
    )
    pkg = packages(client, task["id"])[0]
    detail = client.get(f"/api/packages/{pkg['id']}").json()
    assert detail["failure_code"] == "path_invalid"
    assert "../escape.txt" in detail["reason"]
    assert not any(settings.publish_dir.iterdir())


def test_events_endpoint_limit_and_shape(client: TestClient, source: Path) -> None:
    make_package(source / "pkg", "PKG-EV")
    task = client.post("/api/tasks", json={"directory": str(source)}).json()
    wait_for(
        lambda: [p for p in packages(client, task["id"]) if p["state"] == "published"]
        or None
    )
    events = client.get(f"/api/tasks/{task['id']}/events?limit=5").json()
    assert len(events) <= 5
    assert all({"level", "code", "message", "created_at"} <= set(e) for e in events)
