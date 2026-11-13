#!/usr/bin/env python3
"""Complete the "still being written" demo package.

Usage:
    python -I scripts/finish_writing.py demo/incoming/02-writing

Rebuilds PKG-2026-002.zip in full, then rewrites manifest.json so its
``zip_sha256`` matches. The running task notices the changed files, waits for
two stable scans, and publishes the package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_demo import SAMPLE_FILES, build_zip, sha256_file  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    folder = Path(sys.argv[1]).resolve()
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        print(f"找不到清单：{manifest_path}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    zip_path = folder / manifest["zip"]
    build_zip(zip_path, SAMPLE_FILES)
    manifest["zip_sha256"] = sha256_file(zip_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写入完整压缩包 {zip_path.name}，并更新清单散列。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
