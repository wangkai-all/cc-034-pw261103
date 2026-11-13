"""Package discovery, manifest validation, ZIP inspection and staging.

Manifest semantics
------------------
``manifest.json`` sits next to the ZIP inside each delivery directory::

    {
      "package_code": "PKG-001",
      "zip": "PKG-001.zip",
      "zip_sha256": "<sha256 of the zip file>",
      "files": [
        {"path": "relative/path/inside/zip", "bytes": 123, "sha256": "..."},
        ...
      ]
    }

Paths in ``files`` are relative to the ZIP root, never absolute.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .safe_paths import (
    PathViolation,
    ancestors_of,
    casefold_key,
    detect_case_collision,
    normalise_rel_path,
    zip_member_is_symlink,
)

CHUNK = 1024 * 1024
MANIFEST_NAME = "manifest.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

TERMINAL_PACKAGE_STATES = frozenset(
    {"linked", "published", "failed", "conflict", "discarded"}
)

# package.state values, in pipeline order
STATE_WAITING = "waiting_stable"
STATE_RECEIVING = "receiving"
STATE_RECEIVED = "received"
STATE_VALIDATING = "validating"
STATE_EXTRACTING = "extracting"
STATE_VERIFYING = "verifying"
STATE_PUBLISHING = "publishing"

_PIPELINE_RESUME = {
    STATE_RECEIVED: "received",
    STATE_VALIDATING: "received",
    STATE_EXTRACTING: "received",
    STATE_VERIFYING: "received",
    STATE_PUBLISHING: "received",
}


# ---------------------------------------------------------------------------
# data structures
# ---------------------------------------------------------------------------


@dataclass
class Failure:
    code: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ManifestEntry:
    rel_path: str
    byte_size: int
    sha256: str
    sort_order: int


@dataclass
class Manifest:
    package_code: str
    zip_name: str
    zip_sha256: str
    entries: list[ManifestEntry]


@dataclass
class ZipMember:
    name: str
    byte_size: int


@dataclass
class ZipInspection:
    member_count: int
    total_uncompressed: int
    max_member_bytes: int
    members: list[ZipMember]


@dataclass
class ExtractedFile:
    rel_path: str
    byte_size: int
    sha256: str


Snapshot = tuple[int, int, int]


# ---------------------------------------------------------------------------
# hashing / discovery
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def discover_packages(source_dir: Path) -> list[Path]:
    """Immediate subdirectories of source_dir; each one is a candidate."""
    entries = sorted(os.scandir(source_dir), key=lambda e: e.name)
    return [
        Path(entry.path)
        for entry in entries
        if entry.is_dir(follow_symlinks=False)
    ]


def resolve_source_dir(raw: str) -> Path:
    """Validate a user supplied directory path."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("目录路径不能为空")
    if "\x00" in value:
        raise ValueError("目录路径包含非法字符")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("请输入绝对路径（例如 /data/incoming）")
    # Reject a symlinked entry point before it is resolved away.
    if path.is_symlink():
        raise ValueError(f"不接受符号链接目录：{path}")
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"目录不存在：{path}")
    if not path.is_dir():
        raise ValueError(f"不是目录：{path}")
    return path


def read_snapshot(folder: Path) -> dict[str, Snapshot]:
    """Size/mtime/inode for every non-symlink regular file in the folder."""
    snapshot: dict[str, Snapshot] = {}
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return snapshot
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            snapshot[entry.name] = (st.st_size, st.st_mtime_ns, st.st_ino)
        except OSError:
            snapshot[entry.name] = (-1, -1, -1)
    return snapshot


def same_sizes(previous: dict[str, Snapshot], current: dict[str, Snapshot]) -> bool:
    """Every file present in both scans, with identical sizes, and no new files."""
    if set(previous) != set(current):
        return False
    return all(previous[name][0] == current[name][0] for name in current)


# ---------------------------------------------------------------------------
# manifest validation
# ---------------------------------------------------------------------------

_REQUIRED_TOP_KEYS = ("package_code", "zip", "zip_sha256", "files")


def _fail(code: str, reason: str, **detail: Any) -> tuple[None, Failure]:
    return None, Failure(code, reason, detail)


def peek_zip_name(path: Path, max_manifest_bytes: int) -> str | None:
    """Best-effort read of ``zip`` so the file can be copied strictly.

    Failures here are never final: the staged manifest is validated again.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > max_manifest_bytes:
            return None
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("zip")
    if not isinstance(name, str):
        return None
    try:
        return normalise_rel_path(name)
    except PathViolation:
        return None


def load_manifest(
    path: Path, max_manifest_bytes: int
) -> tuple[Manifest | None, Failure | None]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return _fail("manifest_missing", "缺少 manifest.json")
    except OSError as exc:
        return _fail("manifest_unreadable", f"无法读取 manifest.json：{exc}")

    if st.st_size == 0:
        return _fail("manifest_missing", "manifest.json 为空文件（可能仍在写入）")
    if st.st_size > max_manifest_bytes:
        return _fail(
            "manifest_too_large",
            f"manifest.json 超过大小限制（{max_manifest_bytes} 字节）",
        )

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _fail("manifest_unreadable", f"无法读取 manifest.json：{exc}")

    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _fail("manifest_malformed", "manifest.json 不是 UTF-8 文本")
    except json.JSONDecodeError as exc:
        return _fail(
            "manifest_malformed",
            f"manifest.json 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）",
            error=exc.msg,
        )

    if not isinstance(data, dict):
        return _fail("manifest_malformed", "manifest.json 顶层必须是对象")

    missing_fields = [k for k in _REQUIRED_TOP_KEYS if k not in data]
    if missing_fields:
        return _fail(
            "manifest_fields", f"manifest.json 缺少字段：{', '.join(missing_fields)}"
        )

    code = data.get("package_code")
    if not isinstance(code, str) or not PACKAGE_CODE_RE.match(code.strip()):
        return _fail(
            "manifest_fields", "package_code 必须是安全的交付编号（字母数字 ._-）"
        )
    code = code.strip()

    zip_name = data.get("zip")
    if not isinstance(zip_name, str) or not zip_name.strip():
        return _fail("manifest_fields", "zip 必须是压缩包文件名")
    try:
        zip_rel = normalise_rel_path(zip_name)
    except PathViolation as exc:
        return _fail("manifest_fields", f"zip 字段不安全：{exc.reason}")
    if len(PurePosixPath(zip_rel).parts) != 1:
        return _fail("manifest_fields", "zip 必须是交付目录内的单层文件名")
    if casefold_key(zip_rel) == casefold_key(MANIFEST_NAME):
        return _fail("manifest_fields", "zip 不能与 manifest.json 同名")

    zip_hash = data.get("zip_sha256")
    if not isinstance(zip_hash, str) or not SHA256_RE.match(zip_hash.strip().lower()):
        return _fail("manifest_fields", "zip_sha256 必须是 32 位十六进制 SHA-256")
    zip_hash = zip_hash.strip().lower()

    files = data.get("files")
    if not isinstance(files, list) or not files:
        return _fail("manifest_fields", "files 必须是非空数组")

    entries: list[ManifestEntry] = []
    seen: dict[str, int] = {}
    folded: dict[str, str] = {}

    for index, item in enumerate(files):
        if not isinstance(item, dict):
            return _fail("manifest_entry", f"files[{index}] 必须是对象")
        for key in ("path", "bytes", "sha256"):
            if key not in item:
                return _fail("manifest_entry", f"files[{index}] 缺少 {key}")
        try:
            rel = normalise_rel_path(item["path"])
        except PathViolation as exc:
            return _fail(
                "path_invalid",
                f"清单路径不安全：{exc.reason}",
                path=str(item["path"]),
                index=index,
                violation=exc.reason,
            )
        if casefold_key(rel) == casefold_key(MANIFEST_NAME):
            return _fail(
                "reserved_path",
                "压缩包内不能包含 manifest.json（发布时会与清单文件冲突）",
                path=rel,
                index=index,
            )
        if casefold_key(rel) == casefold_key(zip_rel):
            return _fail(
                "reserved_path",
                f"压缩包内不能包含与压缩包同名的文件：{rel}",
                path=rel,
                index=index,
            )
        if rel in seen:
            return _fail(
                "manifest_duplicate",
                f"清单中存在重复路径：{rel}",
                path=rel,
                first_index=seen[rel],
                index=index,
            )
        key = casefold_key(rel)
        if key in folded:
            return _fail(
                "case_collision",
                f"忽略大小写后出现重名路径：{folded[key]} 与 {rel}",
                path=rel,
                other=folded[key],
                index=index,
            )
        seen[rel] = index
        folded[key] = rel

        size = item["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return _fail(
                "manifest_entry",
                f"清单路径 {rel} 的 bytes 必须是非负整数",
                path=rel,
                index=index,
            )
        digest = item["sha256"]
        if not isinstance(digest, str) or not SHA256_RE.match(digest.strip().lower()):
            return _fail(
                "manifest_entry",
                f"清单路径 {rel} 的 sha256 必须是 32 位十六进制",
                path=rel,
                index=index,
            )
        entries.append(ManifestEntry(rel, size, digest.strip().lower(), index))

    all_paths = set(seen)
    collision = detect_case_collision(sorted(all_paths))
    if collision is not None:
        first, second = collision
        return _fail(
            "case_collision",
            f"忽略大小写后路径重名：{first} 与 {second}",
            path=first,
            other=second,
        )
    for rel in all_paths:
        for parent in ancestors_of(rel):
            if parent in all_paths:
                return _fail(
                    "path_invalid",
                    f"路径既是文件又是目录：{parent} / {rel}",
                    path=rel,
                    conflict=parent,
                )

    return Manifest(code, zip_rel, zip_hash, entries), None


# ---------------------------------------------------------------------------
# ZIP inspection (central directory only, nothing extracted yet)
# ---------------------------------------------------------------------------


def inspect_zip(
    zip_path: Path,
    *,
    max_files: int,
    max_single_file_bytes: int,
    max_total_expanded_bytes: int,
) -> tuple[ZipInspection | None, Failure | None]:
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile as exc:
        return _fail("zip_corrupt", f"压缩包不是有效的 ZIP：{exc}")
    except OSError as exc:
        return _fail("zip_unreadable", f"无法打开压缩包：{exc}")

    with zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            return _fail(
                "too_many_files",
                f"压缩包内条目数 {len(infos)} 超过限制 {max_files}",
                count=len(infos),
                limit=max_files,
            )

        dirs: set[str] = set()
        members: dict[str, zipfile.ZipInfo] = {}
        relative_names: list[str] = []
        for info in infos:
            name = info.filename
            is_dir = name.endswith("/")
            raw = name[:-1] if is_dir else name
            try:
                rel = normalise_rel_path(raw)
            except PathViolation as exc:
                return _fail(
                    "path_invalid",
                    f"压缩包路径不安全（{name}）：{exc.reason}",
                    path=name,
                    violation=exc.reason,
                )
            relative_names.append(rel)
            if is_dir:
                if rel in members:
                    return _fail(
                        "zip_duplicate", f"同一条目既是文件又是目录：{rel}", path=rel
                    )
                dirs.add(rel)
                continue
            if rel in members or rel in dirs:
                return _fail("zip_duplicate", f"压缩包内存在重复条目：{rel}", path=rel)
            if zip_member_is_symlink(info.external_attr, info.create_system):
                return _fail("symlink", f"压缩包包含符号链接：{rel}", path=rel)
            members[rel] = info

        collision = detect_case_collision(sorted(relative_names))
        if collision is not None:
            first, second = collision
            return _fail(
                "case_collision",
                f"压缩包内忽略大小写后重名：{first} 与 {second}",
                path=first,
                other=second,
            )

        total = 0
        member_list: list[ZipMember] = []
        for rel, info in members.items():
            if info.flag_bits & 0x1:
                return _fail("zip_encrypted", f"压缩包包含加密条目：{rel}", path=rel)
            size = info.file_size
            if size > max_single_file_bytes:
                return _fail(
                    "file_too_large",
                    f"文件 {rel} 展开后 {size} 字节，超过单文件限制 {max_single_file_bytes}",
                    path=rel,
                    bytes=size,
                    limit=max_single_file_bytes,
                )
            total += size
            if total > max_total_expanded_bytes:
                return _fail(
                    "expanded_too_large",
                    f"压缩包展开后总大小超过限制 {max_total_expanded_bytes} 字节",
                    limit=max_total_expanded_bytes,
                    bytes=total,
                )
            member_list.append(ZipMember(rel, size))

        member_list.sort(key=lambda m: m.name)
        return (
            ZipInspection(
                member_count=len(member_list),
                total_uncompressed=total,
                max_member_bytes=max((m.byte_size for m in member_list), default=0),
                members=member_list,
            ),
            None,
        )


def compare_manifest(manifest: Manifest, inspection: ZipInspection) -> Failure | None:
    expected = {e.rel_path: e for e in manifest.entries}
    actual = {m.name: m for m in inspection.members}

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"缺少 {len(missing)} 项：" + "、".join(missing[:5]))
        if extra:
            parts.append(f"多出 {len(extra)} 项：" + "、".join(extra[:5]))
        return Failure(
            "manifest_mismatch",
            "压缩包内容与清单不一致：" + "；".join(parts),
            {"missing": missing, "extra": extra},
        )

    for name, entry in expected.items():
        member = actual[name]
        if member.byte_size != entry.byte_size:
            return Failure(
                "size_mismatch",
                f"文件 {name} 清单字节数 {entry.byte_size}，压缩包内为 {member.byte_size}",
                {"path": name, "expected": entry.byte_size, "actual": member.byte_size},
            )
    return None


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------


ProgressCallback = Callable[[int, int, str], None]


def copy_zip_to_staging(source_zip: Path, staged_zip: Path) -> tuple[str | None, Failure | None]:
    """Copy the ZIP while hashing; the source is never read again."""
    staged_zip.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with source_zip.open("rb") as src, staged_zip.open("wb") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                dst.write(chunk)
    except FileNotFoundError:
        return None, Failure("zip_missing", f"清单指定的压缩包不存在：{source_zip.name}")
    except OSError as exc:
        return None, Failure("zip_unreadable", f"复制压缩包失败：{exc}")
    return digest.hexdigest(), None


def extract_and_hash(
    zip_path: Path,
    manifest: Manifest,
    dest_root: Path,
    *,
    max_single_file_bytes: int,
    max_total_expanded_bytes: int,
    max_files: int,
    progress: ProgressCallback | None = None,
) -> tuple[list[ExtractedFile] | None, Failure | None]:
    """Extract every member under dest_root, hashing while streaming.

    Limits are enforced again during extraction because central-directory
    sizes cannot be trusted.
    """
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        return None, Failure("zip_corrupt", f"无法重新打开压缩包：{exc}")

    expected_names = {e.rel_path: e for e in manifest.entries}
    extracted: dict[str, ExtractedFile] = {}
    total = 0
    done = 0
    member_total = max(len(manifest.entries), 1)

    with zf:
        infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
        if len(infos) > max_files:
            return None, Failure(
                "too_many_files", f"解压条目数 {len(infos)} 超过限制 {max_files}"
            )

        for info in infos:
            try:
                rel = normalise_rel_path(info.filename)
            except PathViolation as exc:
                return None, Failure(
                    "path_invalid",
                    f"解压时路径不安全（{info.filename}）：{exc.reason}",
                    {"path": info.filename},
                )
            if zip_member_is_symlink(info.external_attr, info.create_system):
                return None, Failure("symlink", f"解压时发现符号链接：{rel}")
            entry = expected_names.get(rel)
            if entry is None:
                return None, Failure(
                    "manifest_mismatch",
                    f"解压时发现清单之外的文件：{rel}",
                    {"path": rel},
                )

            dest = dest_root / PurePosixPath(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink():
                return None, Failure("symlink", f"暂存目录出现符号链接：{rel}")

            digest = hashlib.sha256()
            written = 0
            try:
                with zf.open(info, "r") as src, dest.open("xb") as dst:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_single_file_bytes:
                            return None, Failure(
                                "file_too_large",
                                f"文件 {rel} 解压过程中超过单文件限制 {max_single_file_bytes}",
                                {"path": rel, "limit": max_single_file_bytes},
                            )
                        total += len(chunk)
                        if total > max_total_expanded_bytes:
                            return None, Failure(
                                "expanded_too_large",
                                f"解压过程中总展开大小超过限制 {max_total_expanded_bytes}",
                                {"limit": max_total_expanded_bytes},
                            )
                        digest.update(chunk)
                        dst.write(chunk)
                        if progress is not None:
                            progress(done, member_total, rel)
            except FileExistsError:
                return None, Failure("zip_duplicate", f"解压时重复条目：{rel}")
            except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
                return None, Failure("zip_corrupt", f"解压 {rel} 失败：{exc}")
            except OSError as exc:
                return None, Failure("extract_failed", f"写入暂存文件失败：{rel}：{exc}")

            done += 1
            extracted[rel] = ExtractedFile(rel, written, digest.hexdigest())
            if progress is not None:
                progress(done, member_total, rel)

    missing = set(expected_names) - set(extracted)
    if missing:
        return None, Failure(
            "manifest_mismatch",
            "清单登记的文件未能从压缩包解出：" + "、".join(sorted(missing)[:5]),
            {"missing": sorted(missing)},
        )

    return [extracted[e.rel_path] for e in manifest.entries], None


def verify_extracted(
    manifest: Manifest, extracted: list[ExtractedFile]
) -> tuple[list[dict[str, Any]], Failure | None]:
    """Per-file check results for the UI."""
    by_path = {f.rel_path: f for f in extracted}
    results: list[dict[str, Any]] = []
    for entry in manifest.entries:
        actual = by_path.get(entry.rel_path)
        row: dict[str, Any] = {
            "rel_path": entry.rel_path,
            "expected_bytes": entry.byte_size,
            "expected_sha256": entry.sha256,
            "actual_bytes": actual.byte_size if actual else None,
            "actual_sha256": actual.sha256 if actual else None,
            "sort_order": entry.sort_order,
        }
        if actual is None:
            row["result"] = "missing"
            row["reason"] = "未解出"
        elif actual.byte_size != entry.byte_size:
            row["result"] = "size_mismatch"
            row["reason"] = f"字节数不符：清单 {entry.byte_size}，实际 {actual.byte_size}"
        elif actual.sha256 != entry.sha256:
            row["result"] = "hash_mismatch"
            row["reason"] = "SHA-256 不符"
        else:
            row["result"] = "ok"
            row["reason"] = None
        results.append(row)

    bad = next((r for r in results if r["result"] != "ok"), None)
    if bad is not None:
        return results, Failure(
            "content_mismatch",
            f"文件校验失败：{bad['rel_path']}（{bad['reason']}）",
            {"path": bad["rel_path"], "result": bad["result"]},
        )
    return results, None


# ---------------------------------------------------------------------------
# filesystem helpers
# ---------------------------------------------------------------------------


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_publish(source: Path, target: Path) -> None:
    """Move a fully built directory into place; never leaves a partial tree."""
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"发布目标已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
        fsync_dir(target.parent)
        return
    except OSError:
        pass
    tmp_root = Path(tempfile.mkdtemp(prefix=".publish-", dir=str(target.parent)))
    try:
        shutil.copytree(source, tmp_root / target.name, symlinks=False)
        os.rename(tmp_root / target.name, target)
        fsync_dir(target.parent)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
