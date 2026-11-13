import { formatBytes, formatTime, shortHash } from "../api";
import { StatusPill } from "./StatusPill";
import type { BatchSummary } from "../types";

interface Props {
  batches: BatchSummary[];
  onOpen: (id: number) => void;
  activeId: number | null;
}

export function BatchList({ batches, onOpen, activeId }: Props) {
  if (batches.length === 0) {
    return (
      <div className="empty-state">
        <h3>还没有批次</h3>
        交付包通过全部检查并发布后会生成批次，可以逐个查看文件校验结果。
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: 16 }}>
      {batches.map((batch) => (
        <div
          key={batch.id}
          className={`batch-card ${batch.state}`}
          style={{
            cursor: "pointer",
            outline: batch.id === activeId ? "2px solid var(--accent)" : undefined,
          }}
          onClick={() => onOpen(batch.id)}
        >
          <div className="batch-head">
            <h3>#{batch.id}</h3>
            <StatusPill state={batch.state} label={batch.state_label} />
            <span className="mono">{batch.package_code}</span>
            {batch.version > 1 && <span className="hint">v{batch.version}</span>}
            <div style={{ flex: 1 }} />
            <span className="hint">{formatTime(batch.updated_at)}</span>
          </div>
          <div className="batch-meta">
            <span>文件 <b>{batch.file_count}</b></span>
            <span>总大小 <b>{formatBytes(batch.total_bytes)}</b></span>
            <span>ZIP 散列 <b>{shortHash(batch.zip_sha256)}</b></span>
          </div>
          {batch.publish_path && (
            <div className="hint mono" style={{ wordBreak: "break-all" }}>
              {batch.publish_path}
            </div>
          )}
          {batch.publish_reason && (
            <div className="hint" style={{ color: "var(--warn)" }}>
              {batch.publish_reason}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
