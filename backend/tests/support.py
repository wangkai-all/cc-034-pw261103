"""Helpers that build delivery directories for tests."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

SAMPLE_FILES: dict[str, bytes] = {
    "README.txt": "hello\n".encode(),
    "data/orders.csv": "id,amount\n1,10\n2,20\n".encode(),
    "config/settings.json": b'{"batch": "daily"}',
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256(path.read_bytes())


def write_zip(path: Path, files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)


def make_manifest(
    code: str,
    zip_name: str,
    files: dict[str, bytes],
    *,
    zip_sha256: str | None = None,
    override_paths: dict[str, Any] | None = None,
    drop_paths: set[str] | None = None,
    extra_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entries = []
    for name, data in files.items():
        if drop_paths and name in drop_paths:
            continue
        entry = {"path": name, "bytes": len(data), "sha256": sha256(data)}
        if override_paths and name in override_paths:
            entry.update(override_paths[name])
        entries.append(entry)
    if extra_entries:
        entries.extend(extra_entries)
    return {
        "package_code": code,
        "zip": zip_name,
        "zip_sha256": zip_sha256 or "0" * 64,
        "files": entries,
    }


def make_package(
    folder: Path,
    code: str,
    files: dict[str, bytes] | None = None,
    *,
    zip_name: str | None = None,
    manifest: dict[str, Any] | None = None,
    zip_bytes: dict[str, bytes] | None = None,
) -> Path:
    """Write a complete, normally valid delivery directory."""
    folder.mkdir(parents=True, exist_ok=True)
    files = files if files is not None else dict(SAMPLE_FILES)
    zip_name = zip_name or f"{code}.zip"
    zip_path = folder / zip_name
    write_zip(zip_path, zip_bytes if zip_bytes is not None else files)
    if manifest is None:
        manifest = make_manifest(
            code, zip_name, files, zip_sha256=sha256_file(zip_path)
        )
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return folder


def write_conflict_package(folder: Path, code: str) -> Path:
    files = dict(SAMPLE_FILES)
    files["README.txt"] = b"a different payload with the same code\n"
    return make_package(folder, code, files)


def make_zip_with_members(folder: Path, code: str, members: dict[str, bytes],
                          *, entries: dict[str, bytes] | None = None) -> Path:
    """ZIP whose raw member names may be unsafe (path traversal, symlink...)."""
    folder.mkdir(parents=True, exist_ok=True)
    zip_name = f"{code}.zip"
    zip_path = folder / zip_name
    write_zip(zip_path, entries if entries is not None else SAMPLE_FILES)
    with zipfile.ZipFile(zip_path, "a") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    manifest = make_manifest(
        code, zip_name, entries if entries is not None else SAMPLE_FILES,
        zip_sha256=sha256_file(zip_path),
    )
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return folder
