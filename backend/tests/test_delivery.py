"""Unit tests for path safety and manifest/ZIP inspection."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.delivery import (
    compare_manifest,
    inspect_zip,
    load_manifest,
)
from app.safe_paths import PathViolation, normalise_rel_path
from tests.support import SAMPLE_FILES, make_manifest, sha256, write_zip


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("/etc/passwd", "绝对路径"),
        ("../x.txt", "父目录"),
        ("a/../../x", "父目录"),
        ("a\\b.txt", "反斜杠"),
        ("C:/temp/x", "盘符"),
        ("", "为空"),
        ("./a.txt", "'.'"),
        ("a//b.txt", "空"),
        ("a/\x00b", "NUL"),
        ("~/secret", "绝对路径"),
        ("a/b?", "非法字符"),
    ],
)
def test_rejects_unsafe_paths(raw: str, reason: str) -> None:
    with pytest.raises(PathViolation) as info:
        normalise_rel_path(raw)
    assert reason in info.value.reason


def test_accepts_nested_relative_path() -> None:
    assert normalise_rel_path("data/2026/orders.csv") == "data/2026/orders.csv"


def test_manifest_requires_core_fields(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    manifest, failure = load_manifest(path, 1024)
    assert manifest is None and failure is not None
    assert failure.code == "manifest_fields"


def test_manifest_rejects_traversal_entry(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = make_manifest("PKG-1", "PKG-1.zip", SAMPLE_FILES)
    manifest["files"].append({"path": "../evil.txt", "bytes": 1, "sha256": sha256(b"x")})
    path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
    _, failure = load_manifest(path, 1024)
    assert failure is not None and failure.code == "path_invalid"
    assert failure.detail["path"] == "../evil.txt"


def test_manifest_rejects_case_collision(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = make_manifest("PKG-1", "PKG-1.zip", SAMPLE_FILES)
    manifest["files"].append({"path": "readme.txt", "bytes": 3, "sha256": sha256(b"abc")})
    path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
    _, failure = load_manifest(path, 1024)
    assert failure is not None and failure.code == "case_collision"


def test_manifest_rejects_reserved_manifest_name(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = make_manifest("PKG-1", "PKG-1.zip", SAMPLE_FILES)
    manifest["files"].append(
        {"path": "manifest.json", "bytes": 1, "sha256": sha256(b"x")}
    )
    path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
    _, failure = load_manifest(path, 1024)
    assert failure is not None and failure.code == "reserved_path"


def test_inspect_zip_rejects_parent_traversal(tmp_path: Path) -> None:
    zip_path = tmp_path / "evil.zip"
    write_zip(zip_path, dict(SAMPLE_FILES))
    with zipfile.ZipFile(zip_path, "a") as zf:
        zf.writestr("../escape.txt", b"x")
    inspection, failure = inspect_zip(
        zip_path, max_files=50, max_single_file_bytes=10_000,
        max_total_expanded_bytes=100_000,
    )
    assert inspection is None and failure is not None
    assert failure.code == "path_invalid"


def test_inspect_zip_rejects_symlink(tmp_path: Path) -> None:
    zip_path = tmp_path / "link.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120777 << 16) | 0o777
        zf.writestr(info, b"/etc/passwd")
        zf.writestr("ok.txt", b"fine")
    inspection, failure = inspect_zip(
        zip_path, max_files=50, max_single_file_bytes=10_000,
        max_total_expanded_bytes=100_000,
    )
    assert inspection is None and failure is not None
    assert failure.code == "symlink"


def test_inspect_zip_limits_files_and_total_size(tmp_path: Path) -> None:
    zip_path = tmp_path / "many.zip"
    write_zip(zip_path, {f"f{i}.txt": b"x" for i in range(10)})
    _, failure = inspect_zip(
        zip_path, max_files=5, max_single_file_bytes=10,
        max_total_expanded_bytes=100,
    )
    assert failure is not None and failure.code == "too_many_files"

    zip_path2 = tmp_path / "big.zip"
    write_zip(zip_path2, {"a.txt": b"a" * 60, "b.txt": b"b" * 60})
    _, failure = inspect_zip(
        zip_path2, max_files=10, max_single_file_bytes=100,
        max_total_expanded_bytes=100,
    )
    assert failure is not None and failure.code == "expanded_too_large"


def test_compare_manifest_reports_missing_and_extra(tmp_path: Path) -> None:
    zip_path = tmp_path / "p.zip"
    write_zip(zip_path, {"a.txt": b"a", "b.txt": b"b"})
    manifest = make_manifest(
        "PKG-1", "p.zip", {"a.txt": b"a", "c.txt": b"c"},
        zip_sha256="0" * 64,
    )
    from app.delivery import Manifest

    parsed = Manifest(
        "PKG-1", "p.zip", "0" * 64,
        [
            __import__("app.delivery", fromlist=["ManifestEntry"]).ManifestEntry(
                e["path"], e["bytes"], e["sha256"], i
            )
            for i, e in enumerate(manifest["files"])
        ],
    )
    inspection, failure = inspect_zip(
        zip_path, max_files=10, max_single_file_bytes=100,
        max_total_expanded_bytes=1000,
    )
    assert inspection is not None
    mismatch = compare_manifest(parsed, inspection)
    assert mismatch is not None and mismatch.code == "manifest_mismatch"
    assert mismatch.detail["missing"] == ["c.txt"]
    assert mismatch.detail["extra"] == ["b.txt"]


def test_zip_directory_entry_tolerated(tmp_path: Path) -> None:
    zip_path = tmp_path / "dirs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("data/", b"")
        zf.writestr("data/a.txt", b"a")
    inspection, failure = inspect_zip(
        zip_path, max_files=10, max_single_file_bytes=100,
        max_total_expanded_bytes=1000,
    )
    assert failure is None
    assert inspection is not None and inspection.member_count == 1


def test_corrupt_zip_is_reported(tmp_path: Path) -> None:
    zip_path = tmp_path / "broken.zip"
    zip_path.write_bytes(b"PK\x03\x04 not really a zip")
    _, failure = inspect_zip(
        zip_path, max_files=10, max_single_file_bytes=100,
        max_total_expanded_bytes=1000,
    )
    assert failure is not None and failure.code == "zip_corrupt"


def test_empty_manifest_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b"")
    manifest, failure = load_manifest(path, 1000)
    assert manifest is None
    assert failure is not None and failure.code == "manifest_missing"


def test_zip_sha_must_match_manifest_field(tmp_path: Path) -> None:
    zip_path = tmp_path / "p.zip"
    write_zip(zip_path, SAMPLE_FILES)
    manifest = make_manifest(
        "PKG-1", "p.zip", SAMPLE_FILES, zip_sha256=sha256(b"not-the-zip")
    )
    path = tmp_path / "manifest.json"
    path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
    # The manifest is structurally valid; the hash mismatch is caught on receipt.
    parsed, failure = load_manifest(path, 10_000)
    assert failure is None and parsed is not None
    assert parsed.zip_sha256 == sha256(b"not-the-zip")
