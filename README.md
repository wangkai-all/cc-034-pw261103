# 交付包接收台（Delivery Console）

指定一个服务端本地目录，应用会自动发现其中的**新交付包**：每个交付包是一个子目录，内含
`manifest.json` 和一个 ZIP。网页上输入目录路径，即可实时查看扫描与接收进度、逐文件校验结果，
并在接收完成后预览或下载文件。

- 后端：Python 3.11 · FastAPI · SQLite（标准库 `sqlite3`）
- 前端：React 18 · TypeScript · Vite（无第三方状态管理/UI 库）

## 快速开始

```bash
# 1) 后端依赖
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# 2) 前端构建（产物由后端直接托管）
cd frontend && npm install && npm run build && cd ..

# 3) 生成演示数据（可选）
.venv/bin/python -I scripts/make_demo.py demo/incoming

# 4) 启动
./scripts/run.sh            # http://127.0.0.1:8000
```

开发模式下前后端分开跑：`./scripts/run.sh`（后端 8000）加
`cd frontend && npm run dev`（前端 5173，已配置 `/api` 代理）。

网页中填入演示目录的绝对路径（如 `/workspace/demo/incoming`）并点击「开始扫描」。

## 交付包约定

```jsonc
{
  "package_code": "PKG-2026-001",     // 交付编号
  "zip": "PKG-2026-001.zip",          // 交付目录内的压缩包名（单层文件名）
  "zip_sha256": "…",                  // ZIP 文件的 SHA-256
  "files": [
    // 压缩包内各文件的相对路径、字节数、SHA-256（相对 ZIP 根，不允许绝对路径）
    {"path": "data/orders.csv", "bytes": 1041, "sha256": "…"}
  ]
}
```

## 处理流程与规则

1. **发现与稳定判定**：每隔 `SCAN_INTERVAL_SECONDS`（默认 5s）扫描目录的一级子目录，
   记录每个文件的大小/修改时间。只有**连续两次扫描文件大小不变**才进入接收。
2. **接收**：把 `manifest.json` 和 ZIP 复制到**本次任务专属暂存目录**
   （`<data>/staging/task-<id>/pkg-<n>/`），复制时重新计算 ZIP 散列，与清单比对。
   复制期间源目录再有变化则放弃本轮，等待下次稳定。
3. **核对清单**：缺少文件、多出文件、字节数不符、ZIP 散列不符都会给出具体原因；
   失败时正式发布目录保持原样。
4. **解压前检查**：路径（拒绝绝对路径、`..` 跳转、Windows 盘符、反斜杠、NUL 等）、
   条目数、单文件展开大小、总展开大小、符号链接、忽略大小写后重名（含目录级）。
5. **解压中再限制**：流式解压时再次限制单文件与总展开大小（中央目录可能谎报大小）。
6. **逐文件校验**：解压出的每个文件重新计算 SHA-256，与清单逐项比对。
7. **发布**：全部通过后先在任务目录内组装完整目录树，再用 `os.rename` **原子**移动到
   `<publish>/<package_code>`。目标已存在则失败且不覆盖；任何一步失败都不会留下半份数据。
8. **去重与冲突**：
   - 同一任务内 **编号相同且 ZIP 散列相同** → 关联到已有批次（`linked`），不重复发布；
   - 编号相同但散列不同 → `conflict`，在「冲突」页由用户选择：
     *保留已有批次*（丢弃本次内容，正式目录不变）或 *接收本次内容*
     （原子替换发布目录，原批次标记为 `superseded`）。
9. **失败记录**：失败的交付包保留状态、原因与逐文件检查行（未解压时标记为「未校验」），
   可在任务详情查看；同时写入事件日志。

## 演示数据

`demo/incoming` 已随仓库提供（约 160 KB）；需要重新生成时运行
`.venv/bin/python -I scripts/make_demo.py demo/incoming`，会重建 7 个交付目录。

| 目录 | 场景 | 预期结果 |
| --- | --- | --- |
| `01-normal` | 正常包 | 校验通过并发布 |
| `02-writing` | 压缩包被截断（写入未完成） | `zip_hash_mismatch`，不发布 |
| `03-mismatch` | 清单散列错误 + 压缩包多出未登记文件 | `manifest_mismatch`，不发布 |
| `04-paths` | ZIP 内含 `../escape.txt` 与绝对路径 | 解压前即 `path_invalid` |
| `05-bomb` | 5 个 30 MB 文件（合计 150 MB > 128 MB 上限） | `expanded_too_large` |
| `06-duplicate` | 与 `01-normal` 同编号同内容 | `linked` 到已有批次 |
| `07-conflict` | 与 `01-normal` 同编号不同内容 | `conflict`，等待用户处理 |

完成“正在写入”的包，让它随后顺利发布：

```bash
.venv/bin/python -I scripts/finish_writing.py demo/incoming/02-writing
```

任务会在下一轮发现文件变化，待稳定后重新接收并发布（也可在详情里点「立即扫描」）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_DATA_DIR` | `./.data` | 数据库与发布/暂存目录根 |
| `APP_DB_PATH` | `<data>/app.sqlite3` | SQLite 路径 |
| `APP_PUBLISH_DIR` | `<data>/published` | 正式发布目录 |
| `APP_STAGING_DIR` | `<data>/staging` | 任务暂存目录 |
| `SCAN_INTERVAL_SECONDS` | `5` | 扫描间隔 |
| `MAX_SINGLE_FILE_BYTES` | `33554432` | 单文件展开上限（32 MB） |
| `MAX_TOTAL_EXPANDED_BYTES` | `134217728` | 整包展开上限（128 MB） |
| `MAX_FILES_PER_PACKAGE` | `500` | 条目数上限 |
| `MAX_MANIFEST_BYTES` | `8388608` | 清单大小上限 |

上限在任务创建时写入任务配置并展示在页面上。

## 测试

```bash
.venv/bin/python -m pytest backend/tests -q      # 后端：单元 + 全流程 + HTTP API
```

UI 冒烟测试（jsdom 加载真实构建产物 + 真实后端，需先构建前端并启动服务、创建演示任务）：

```bash
node frontend/tests/ui-smoke.mjs
```

## 目录结构

```
backend/app/
  main.py        FastAPI 路由：任务、交付包、批次、冲突、预览/下载
  worker.py      每任务一个线程：扫描→接收→核对→解压校验→发布
  delivery.py    清单解析、ZIP 检查、流式解压与散列、原子发布
  safe_paths.py  路径规范化与越界/重名检测
  db.py          SQLite 表结构与访问层
frontend/src/    React 页面（App.tsx）与组件
scripts/         演示数据生成、补完写入、启动脚本
```
