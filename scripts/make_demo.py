"""Demo data generator: creates a source directory with seven deliveries."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_zip(zip_path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)


def manifest_for(code: str, zip_name: str, zip_path: Path, files: dict[str, bytes],
                 *, overrides: dict[str, dict] | None = None,
                 extra_entries: list[dict] | None = None) -> dict:
    entries = []
    for name, data in files.items():
        entry = {"path": name, "bytes": len(data), "sha256": sha256_bytes(data)}
        if overrides and name in overrides:
            entry.update(overrides[name])
        entries.append(entry)
    if extra_entries:
        entries.extend(extra_entries)
    return {
        "package_code": code,
        "zip": zip_name,
        "zip_sha256": sha256_file(zip_path),
        "files": entries,
    }


SAMPLE_FILES: dict[str, bytes] = {
    "README.txt": (
        "订阅数据交付样例\n"
        "================\n"
        "本包用于演示交付接收台的完整流程。\n"
        "包含一份订单 CSV、一份说明文档与一份配置。\n"
    ).encode("utf-8"),
    "data/orders.csv": (
        "order_id,customer,amount,currency\n"
        + "".join(f"{1000+i},客户{i:03d},{i*3.25:.2f},CNY\n" for i in range(1, 41))
    ).encode("utf-8"),
    "config/settings.json": json.dumps(
        {"batch": "daily", "encoding": "utf-8", "columns": ["order_id", "amount"]},
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8"),
    "docs/说明.md": (
        "# 交付说明\n\n"
        "- 数据来源：样例\n"
        "- 更新频率：每日\n"
        "- 校验方式：SHA-256\n"
    ).encode("utf-8"),
}


def write_package(folder: Path, code: str, files: dict[str, bytes],
                  manifest_data: dict | None = None,
                  *, write_zip: bool = True, zip_name: str | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    zip_name = zip_name or f"{code}.zip"
    zip_path = folder / zip_name
    if write_zip:
        build_zip(zip_path, files)
    if manifest_data is None:
        manifest_data = manifest_for(code, zip_name, zip_path, files)
    (folder / "manifest.json").write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def create_demo(root: Path) -> dict[str, str]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    summary: dict[str, str] = {}

    # 1. A well-formed package that should publish.
    write_package(root / "01-normal", "PKG-2026-001", SAMPLE_FILES)
    summary["01-normal"] = "正常包：应完成接收、解压、校验并发布。"

    # 2. A package whose ZIP is still being written.
    writing = root / "02-writing"
    writing.mkdir()
    zip_path = writing / "PKG-2026-002.zip"
    build_zip(zip_path, SAMPLE_FILES)
    # The manifest describes the finished archive, then the archive is cut short
    # to mimic a writer that has not finished streaming the central directory.
    manifest = manifest_for("PKG-2026-002", "PKG-2026-002.zip", zip_path, SAMPLE_FILES)
    (writing / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data = zip_path.read_bytes()
    cut = max(1024, len(data) // 2)
    zip_path.write_bytes(data[:cut])
    summary["02-writing"] = "正在写入的包：压缩包被截断，清单声明的散列与当前文件不符。"

    # 3. Manifest mismatch: one entry has a wrong hash and one file is unlisted.
    mismatch_files = dict(SAMPLE_FILES)
    mismatch_files["data/extra.csv"] = b"id,value\n1,42\n2,43\n"
    tmp_dir = root / ".tmp-mismatch"
    tmp_dir.mkdir()
    build_zip(tmp_dir / "PKG-2026-003.zip", mismatch_files)
    manifest = manifest_for("PKG-2026-003", "PKG-2026-003.zip",
                            tmp_dir / "PKG-2026-003.zip", SAMPLE_FILES,
                            overrides={"data/orders.csv": {"sha256": "0" * 64}})
    write_package(root / "03-mismatch", "PKG-2026-003", mismatch_files,
                  manifest_data=manifest)
    shutil.rmtree(tmp_dir)
    summary["03-mismatch"] = "清单不符：订单文件散列错误，且压缩包内多出一个未登记文件。"

    # 4. Path traversal / absolute paths inside the ZIP.
    zip_name = "PKG-2026-004.zip"
    folder = root / "04-paths"
    folder.mkdir()
    zip_path = folder / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in SAMPLE_FILES.items():
            zf.writestr(name, data)
        zf.writestr("../escape.txt", b"should never be written\n")
        zf.writestr("/abs/path.txt", b"absolute path\n")
    # The manifest itself looks clean: the unsafe entries only surface when
    # the archive is inspected before extraction.
    manifest = {
        "package_code": "PKG-2026-004",
        "zip": zip_name,
        "zip_sha256": sha256_file(zip_path),
        "files": [
            {"path": n, "bytes": len(d), "sha256": sha256_bytes(d)}
            for n, d in SAMPLE_FILES.items()
        ],
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary["04-paths"] = "路径越界：压缩包内含 ../ 跳转与绝对路径条目，应在解压前被拒绝。"

    # 5. Expansion limit: a highly compressible file that grows past the cap.
    big_files = dict(SAMPLE_FILES)
    # Five 30 MB members: each stays under the 32 MB per-file cap, but their
    # total exceeds the default 128 MB expanded limit.
    payload = (b"A" * 1024) * 1024 * 30
    for index in range(1, 6):
        big_files[f"data/part-{index}.log"] = payload
    zip_name = "PKG-2026-005.zip"
    folder = root / "05-bomb"
    folder.mkdir()
    zip_path = folder / zip_name
    build_zip(zip_path, big_files)
    manifest = manifest_for("PKG-2026-005", zip_name, zip_path, big_files)
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary["05-bomb"] = "展开超限：5 个 30 MB 文件合计约 150 MB，超过默认总展开大小限制 128 MB。"

    # 6. Duplicate content: same package code and zip hash as #1.
    write_package(root / "06-duplicate", "PKG-2026-001", SAMPLE_FILES)
    summary["06-duplicate"] = "重复内容：与 01-normal 编号及内容完全一致，应只关联既有批次。"

    # 7. Same code, different content: conflict.
    conflict_files = dict(SAMPLE_FILES)
    conflict_files["README.txt"] = b"different content for the same code\n"
    write_package(root / "07-conflict", "PKG-2026-001", conflict_files)
    summary["07-conflict"] = "编号冲突：PKG-2026-001 内容不同，应交给用户处理。"

    readme = root / "README.txt"
    lines = ["演示数据说明", "============", ""]
    for name, text in summary.items():
        lines.append(f"{name}: {text}")
    lines.append("")
    lines.append("02-writing 可运行 scripts/finish_writing.py 补完写入，模拟文件写完后的重新接收。")
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo/incoming")
    summary = create_demo(target)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"demo data written to {target.resolve()}")
