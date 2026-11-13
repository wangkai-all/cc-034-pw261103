import { useState } from "react";
import { api, formatBytes, formatTime, shortHash } from "../api";
import { failureLabel } from "../labels";
import type { ConflictItem } from "../types";

interface Props {
  conflicts: ConflictItem[];
  onResolved: () => void;
}

export function ConflictPanel({ conflicts, onResolved }: Props) {
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function resolve(id: number, resolution: "keep_existing" | "receive_incoming") {
    setBusyId(id);
    setError(null);
    try {
      await api.resolveConflict(id, resolution);
      onResolved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }

  if (conflicts.length === 0) {
    return (
      <div className="empty-state">
        <h3>没有待处理的冲突</h3>
        编号相同但内容不同的交付包会出现在这里，由你决定保留哪一份。
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, padding: 16 }}>
      {error && <div className="notice error">{error}</div>}
      {conflicts.map((item) => (
        <div key={item.id} className="conflict-card">
          <div className="batch-head">
            <h3 style={{ fontSize: 13.5 }} className="mono">
              {item.package_code}
            </h3>
            <span className="pill warn">编号冲突</span>
            <span className="faint mono">交付目录 {item.relative_path}</span>
          </div>
          <div className="hint">
            {item.reason} {item.failure_code && <>（{failureLabel(item.failure_code)}）</>}
          </div>
          <div className="conflict-grid">
            <div className="conflict-side">
              <h4>已有批次（已发布）</h4>
              {item.existing_batch ? (
                <>
                  <div className="batch-meta">
                    <span>批次 <b>#{item.existing_batch.id}</b></span>
                    <span>文件 <b>{item.existing_batch.file_count}</b></span>
                    <span>大小 <b>{formatBytes(item.existing_batch.total_bytes)}</b></span>
                  </div>
                  <div className="hash">SHA-256 {item.existing_batch.zip_sha256}</div>
                  <div className="hint">发布于 {formatTime(item.existing_batch.updated_at)}</div>
                </>
              ) : (
                <div className="hint">未找到已有批次记录。</div>
              )}
            </div>
            <div className="conflict-side" style={{ borderColor: "#f0d29a" }}>
              <h4>本次内容（等待决定）</h4>
              {item.incoming_batch ? (
                <>
                  <div className="batch-meta">
                    <span>批次 <b>#{item.incoming_batch.id}</b></span>
                    <span>文件 <b>{item.incoming_batch.file_count}</b></span>
                    <span>大小 <b>{formatBytes(item.incoming_batch.total_bytes)}</b></span>
                  </div>
                  <div className="hash">SHA-256 {item.incoming_batch.zip_sha256}</div>
                  <div className="hint">校验通过，暂存于本任务目录</div>
                </>
              ) : (
                <div className="hint">本次内容未创建批次。</div>
              )}
            </div>
          </div>
          <ul>
            <li>保留已有批次：本次内容被丢弃，正式目录保持不变。</li>
            <li>接收本次内容：原子替换正式目录，原批次标记为「已替换」。</li>
          </ul>
          <div className="conflict-actions">
            <button
              className="ghost-btn"
              disabled={busyId === item.id}
              onClick={() => resolve(item.id, "keep_existing")}
            >
              保留已有批次（{shortHash(item.existing_batch?.zip_sha256)}）
            </button>
            <button
              className="danger-btn"
              disabled={busyId === item.id}
              onClick={() => resolve(item.id, "receive_incoming")}
            >
              接收本次内容（{shortHash(item.incoming_batch?.zip_sha256)}）
            </button>
            {busyId === item.id && <span className="spinner dark" />}
          </div>
        </div>
      ))}
    </div>
  );
}
