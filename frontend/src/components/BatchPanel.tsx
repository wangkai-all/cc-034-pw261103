import { useEffect, useMemo, useState } from "react";
import { api, formatBytes, formatTime, shortHash } from "../api";
import { RESULT_LABELS, resultTone } from "../labels";
import { StatusPill } from "./StatusPill";
import type { BatchDetail, BatchFile, Preview } from "../types";

interface Props {
  batchId: number | null;
  onClose: () => void;
}

export function BatchPanel({ batchId, onClose }: Props) {
  const [batch, setBatch] = useState<BatchDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<BatchFile | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "failed">("all");

  useEffect(() => {
    let cancelled = false;
    setBatch(null);
    setSelectedFile(null);
    setPreview(null);
    setError(null);
    if (batchId === null) return undefined;

    const load = () => {
      api
        .getBatch(batchId)
        .then((value) => {
          if (!cancelled) setBatch(value);
        })
        .catch((err) => {
          if (!cancelled) setError(err instanceof Error ? err.message : String(err));
        });
    };
    load();
    const timer = window.setInterval(load, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [batchId]);

  const files = useMemo(() => {
    if (!batch) return [];
    return filter === "all"
      ? batch.files
      : batch.files.filter((file) => file.result !== "ok");
  }, [batch, filter]);

  async function openPreview(file: BatchFile) {
    if (!batch) return;
    setSelectedFile(file);
    setPreview(null);
    setPreviewError(null);
    try {
      const value = await api.preview(batch.id, file.id);
      setPreview(value);
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : String(err));
    }
  }

  if (batchId === null) {
    return (
      <div className="empty-state">
        <h3>选择一个批次</h3>
        在批次列表中选择已发布或待处理的批次，查看逐文件校验结果，并预览或下载文件。
      </div>
    );
  }
  if (error) return <div className="notice error" style={{ margin: 16 }}>{error}</div>;
  if (!batch) return <div className="empty-state">正在加载批次…</div>;

  return (
    <div>
      <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border)" }}>
        <div className="batch-head" style={{ marginBottom: 8 }}>
          <h2 style={{ fontSize: 15 }}>批次 #{batch.id}</h2>
          <StatusPill state={batch.state} label={batch.state_label} />
          <span className="mono">{batch.package_code}</span>
          <div style={{ flex: 1 }} />
          <button className="link-btn" onClick={onClose}>
            收起
          </button>
        </div>
        <div className="batch-meta">
          <span>文件 <b>{batch.file_count}</b></span>
          <span>总大小 <b>{formatBytes(batch.total_bytes)}</b></span>
          <span>压缩包 <b>{batch.zip_name ?? "-"}</b></span>
          <span>ZIP 散列 <b>{shortHash(batch.zip_sha256)}</b></span>
          <span>创建于 <b>{formatTime(batch.created_at)}</b></span>
        </div>
        {batch.publish_path && (
          <div className="hint" style={{ marginTop: 6 }}>
            发布路径：<span className="mono">{batch.publish_path}</span>
          </div>
        )}
        {batch.publish_reason && (
          <div className="notice warn" style={{ marginTop: 8 }}>
            {batch.publish_reason}
          </div>
        )}
        <div className="actions-row" style={{ marginTop: 10 }}>
          <a
            className="ghost-btn"
            href={api.zipDownloadUrl(batch.id)}
            style={{ display: "inline-block" }}
          >
            下载 ZIP
          </a>
          <div className="segmented">
            <button
              className={filter === "all" ? "active" : ""}
              onClick={() => setFilter("all")}
            >
              全部 {batch.files.length}
            </button>
            <button
              className={filter === "failed" ? "active" : ""}
              onClick={() => setFilter("failed")}
            >
              异常 {batch.files.filter((file) => file.result !== "ok").length}
            </button>
          </div>
        </div>
      </div>

      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>文件</th>
              <th>结果</th>
              <th>字节</th>
              <th>SHA-256</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {files.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  {filter === "failed" ? "没有异常文件。" : "批次没有文件记录。"}
                </td>
              </tr>
            )}
            {files.map((file) => (
              <tr
                key={file.id}
                className={selectedFile?.id === file.id ? "selected" : ""}
              >
                <td className="path-cell">{file.rel_path}</td>
                <td>
                  <span className={`pill ${resultTone(file.result)}`}>
                    {RESULT_LABELS[file.result] ?? file.result}
                  </span>
                  {file.reason && <div className="hint">{file.reason}</div>}
                </td>
                <td className="mono nowrap">{formatBytes(file.byte_size)}</td>
                <td className="mono">{shortHash(file.sha256)}</td>
                <td>
                  <div className="actions-row">
                    <button
                      className="link-btn"
                      onClick={() => openPreview(file)}
                      disabled={file.result !== "ok"}
                    >
                      预览
                    </button>
                    {file.result === "ok" ? (
                      <a
                        className="link-btn"
                        href={api.downloadUrl(batch.id, file.id)}
                        style={{ fontSize: 12.5 }}
                      >
                        下载
                      </a>
                    ) : (
                      <span className="faint hint">不可下载</span>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {(selectedFile || previewError) && (
        <div style={{ padding: "14px 16px", borderTop: "1px solid var(--border)" }}>
          <PreviewPane
            file={selectedFile}
            preview={preview}
            error={previewError}
          />
        </div>
      )}
    </div>
  );
}

function PreviewPane({
  file,
  preview,
  error,
}: {
  file: BatchFile | null;
  preview: Preview | null;
  error: string | null;
}) {
  if (error) {
    return <div className="notice error">{error}</div>;
  }
  if (!file) return null;
  if (!preview) {
    return (
      <div className="preview-pane">
        <div className="preview-head">
          <span className="path">{file.rel_path}</span>
          <span className="spinner dark" />
        </div>
        <div className="hint">正在读取预览…</div>
      </div>
    );
  }
  return (
    <div className="preview-pane">
      <div className="preview-head">
        <span className="path">{file.rel_path}</span>
        <span className="pill wait">{formatBytes(preview.size)}</span>
      </div>
      {preview.kind === "text" ? (
        file.rel_path.toLowerCase().endsWith(".csv") ? (
          <CsvPreview content={preview.content} />
        ) : (
          <pre className="code-view">{preview.content}</pre>
        )
      ) : (
        <div className="notice info">{preview.message}</div>
      )}
      {preview.kind === "text" && preview.truncated && (
        <div className="hint">内容已截断，完整内容请下载。</div>
      )}
    </div>
  );
}

function CsvPreview({ content }: { content: string }) {
  const rows = content.split(/\r?\n/).filter((line) => line.length > 0);
  if (rows.length === 0) return <div className="hint">空文件</div>;
  const parse = (line: string) => line.split(",");
  const header = parse(rows[0]);
  const body = rows.slice(1, 60);
  return (
    <div className="csv-view">
      <table>
        <thead>
          <tr>
            {header.map((cell, index) => (
              <th key={index}>{cell}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((line, rowIndex) => (
            <tr key={rowIndex}>
              {parse(line).map((cell, cellIndex) => (
                <td key={cellIndex}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length - 1 > body.length && (
        <div className="hint" style={{ padding: "6px 10px" }}>
          仅显示前 {body.length} 行，共 {rows.length - 1} 行。
        </div>
      )}
    </div>
  );
}
