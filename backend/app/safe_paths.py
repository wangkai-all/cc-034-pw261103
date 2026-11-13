"""Safe path handling shared by manifest validation and ZIP extraction."""

from __future__ import annotations

import posixpath
import re

_DRIVE_RE = re.compile(r"^[A-Za-z]:([\\/]|$)")
_FORBIDDEN_CHARS = set("<>:\"|?*")


class PathViolation(Exception):
    """A relative path from the delivery is unsafe to materialise."""

    def __init__(self, reason: str, path: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path


def normalise_rel_path(raw: str | None) -> str:
    """Return a safe, normalised POSIX relative path or raise PathViolation.

    Rejects absolute paths (POSIX and Windows), parent-directory traversal,
    dot segments, NUL bytes, Windows drive letters and characters that cannot
    exist on common filesystems.
    """
    if raw is None or not isinstance(raw, str):
        raise PathViolation("路径不是字符串", repr(raw))
    value = raw.strip()
    if not value:
        raise PathViolation("路径为空", raw)
    if "\x00" in value:
        raise PathViolation("路径包含 NUL 字符", raw)
    if "\\" in value:
        raise PathViolation("路径包含反斜杠（应为 POSIX 相对路径）", raw)
    if _DRIVE_RE.match(value):
        raise PathViolation("拒绝 Windows 盘符绝对路径", raw)
    if value.startswith("/") or value.startswith("~"):
        raise PathViolation("拒绝绝对路径", raw)

    # Collapse repeated separators before inspection of segments.
    parts = value.split("/")
    if any(part == "" for part in parts):
        raise PathViolation("路径包含空的路径段", raw)
    if parts[0] == ".":
        raise PathViolation("拒绝以 '.' 开头的路径", raw)
    for part in parts:
        if part == "..":
            raise PathViolation("拒绝父目录跳转（..）", raw)
        if part in (".", ""):
            raise PathViolation("路径包含 '.' 或空段", raw)
        if part.casefold() in {".", ".."}:
            raise PathViolation("拒绝父目录跳转", raw)
        if any(ch in _FORBIDDEN_CHARS for ch in part):
            raise PathViolation("路径包含非法字符", raw)
        if "\r" in part or "\n" in part or "\t" in part:
            raise PathViolation("路径包含控制字符", raw)

    normalised = posixpath.join(*parts)
    if normalised != value:
        raise PathViolation("路径不是规范形式", raw)
    return normalised


def ancestors_of(rel_path: str) -> list[str]:
    parts = rel_path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def casefold_key(rel_path: str) -> str:
    return rel_path.casefold()


def detect_case_collision(paths: list[str]) -> tuple[str, str] | None:
    """Return the first pair of paths that collide when case is ignored.

    Directory components are included, so ``A/b.txt`` and ``a/c.txt`` collide
    on case-insensitive filesystems even though the leaf names differ.
    """
    folded: dict[str, str] = {}
    for path in paths:
        for candidate in [path, *ancestors_of(path)]:
            key = candidate.casefold()
            existing = folded.get(key)
            if existing is not None and existing != candidate:
                return candidate, existing
            folded[key] = candidate
    return None


def zip_member_is_symlink(external_attr: int, create_system: int) -> bool:
    """ZIP stores Unix mode in the high 16 bits of external_attr."""
    if create_system != 3:  # 3 == Unix
        return False
    mode = (external_attr >> 16) & 0xFFFF
    return (mode & 0o170000) == 0o120000
