import { useEffect, useState } from "react";
import { api, formatBytes, shortHash } from "../api";
import { RESULT_LABELS, failureLabel, resultTone } from "../labels";
import { StatusPill } from "./StatusPill";
import type { BatchSummary, ManifestRow, PackageDetail, PackageSummary } from "../types";

interface Props {
  packageId: number | null;
  summary: PackageSummary | null;
  onOpenBatch: (batchId: number) => void;
  onConflict: (packageId: number) => void;
}

export function PackageDetailPanel({ packageId, summary, onOpenBatch, onConflict }: Props) {
  const [detail, setDetail] = useState<PackageDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    if (packageId === null) return;
    api
      .getPackage(packageId)
      .then((value) => {
        if (!cancelled) setDetail(value);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [packageId, summary?.updated_at]);

  if (packageId === null) {
    return (
      <div className="empty-state">
        <h3>选择一个交付包</h3>
        点击上方表格中的行，查看清单核对结果与失败原因。
      </div>
    );
  }
  if (error) return <div className="notice error" style={{ margin: 16 }}>{error}</div>;
  if (!detail || !summary) {
    return <div className="empty-state">正在加载交付包详情…</div>;
  }

  const entries = detail.entries;
  const failedEntries = entries.filter((row) => row.result !== "ok");
  const batch: BatchSummary | null = detail.batch;

  return (
    <div>
      <div className="detail-split">
        <section>
          <div className="section-title">交付包 #{detail.id}</div>
          <dl className="kv">
            <dt>目录</dt>
            <dd>{detail.source_dir}/{detail.relative_path}</dd>
            <dt>交付编号</dt>
            <dd>{detail.package_code ?? "-"}</dd>
            <dt>当前状态</dt>
            <dd>{detail.state_label}</dd>
            <dt>状态说明</dt>
            <dd style={{ fontFamily: "inherit", fontSize: 12.5 }}>
              {detail.reason ?? "-"}
            </dd>
            {detail.failure_code && (
              <>
                <dt>失败类型</dt>
                <dd style={{ color: "var(--error)" }}>
                  {failureLabel(detail.failure_code)}（{detail.failure_code}）
                </dd>
              </>
            )}
            <dt>压缩包</dt>
            <dd>
              {detail.zip_name ?? "-"}
              {detail.zip_bytes > 0 && <> · {formatBytes(detail.zip_bytes)}</>}
            </dd>
            <dt>ZIP 散列</dt>
            <dd>{detail.zip_sha256 ?? "-"}</dd>
          </dl>

          {detail.objects.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <div className="section-title">源目录内容</div>
              <div className="table-wrap">
                <table className="grid">
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>类型</th>
                      <th>大小</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.objects.map((obj) => (
                      <tr key={obj.name}>
                        <td className="mono">{obj.name}</td>
                        <td>{obj.kind === "file" ? "文件" : obj.kind === "dir" ? "目录" : "符号链接"}</td>
                        <td className="mono nowrap">
                          {obj.kind === "file" ? formatBytes(obj.bytes) : "-"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </section>

        <section>
          <div className="section-title">批次</div>
          {batch ? (
            <div className={`batch-card ${batch.state}`}>
              <div className="batch-head">
                <h3>#{batch.id}</h3>
                <StatusPill state={batch.state} label={batch.state_label} />
                <span className="hint">版本 v{batch.version}</span>
              </div>
              <div className="batch-meta">
                <span>文件 <b>{batch.file_count}</b></span>
                <span>大小 <b>{formatBytes(batch.total_bytes)}</b></span>
                <span>ZIP 散列 <b>{shortHash(batch.zip_sha256)}</b></span>
              </div>
              {batch.publish_path && (
                <div className="hint">发布路径：{batch.publish_path}</div>
              )}
              {batch.publish_reason && (
                <div className="hint" style={{ color: "var(--warn)" }}>
                  {batch.publish_reason}
                </div>
              )}
              <div className="actions-row">
                {(batch.state === "published" ||
                  batch.state === "publishing" ||
                  batch.state === "conflict") && (
                  <button className="ghost-btn" onClick={() => onOpenBatch(batch.id)}>
                    查看文件校验
                  </button>
                )}
                {detail.state === "conflict" && (
                  <button className="danger-btn" onClick={() => onConflict(detail.id)}>
                    处理冲突
                  </button>
                )}
              </div>
            </div>
          ) : (
            <div className="hint">尚未创建批次。校验全部通过并发布后才会生成批次。</div>
          )}
          <div className="hint" style={{ marginTop: 12 }}>
            失败或冲突的交付包不会改动正式数据；已发布目录中的内容不会因为本次失败而变化。
          </div>
        </section>
      </div>

      <div style={{ borderTop: "1px solid var(--border)" }}>
        <section style={{ padding: "14px 16px" }}>
          <div className="section-title">
            清单核对 · {entries.length} 项
            {failedEntries.length > 0 && (
              <span className="pill err" style={{ marginLeft: 4 }}>
                {failedEntries.length} 项异常
              </span>
            )}
          </div>
          {entries.length === 0 ? (
            <div className="hint">尚无逐文件核对结果（刚开始扫描或清单无法解析）。</div>
          ) : (
            <div className="table-wrap">
              <table className="grid">
                <thead>
                  <tr>
                    <th>路径</th>
                    <th>结果</th>
                    <th>清单字节</th>
                    <th>实际字节</th>
                    <th>清单散列</th>
                    <th>实际散列</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((row: ManifestRow) => (
                    <tr key={row.rel_path}>
                      <td className="path-cell">{row.rel_path}</td>
                      <td>
                        <span className={`pill ${resultTone(row.result)}`}>
                          {RESULT_LABELS[row.result] ?? row.result}
                        </span>
                        {row.reason && (
                          <div className="hint" style={{ marginTop: 2 }}>
                            {row.reason}
                          </div>
                        )}
                      </td>
                      <td className="mono nowrap">{row.expected_bytes}</td>
                      <td className="mono nowrap">
                        {row.actual_bytes === null ? "-" : row.actual_bytes}
                      </td>
                      <td className="mono">{shortHash(row.expected_sha256)}</td>
                      <td className="mono">{shortHash(row.actual_sha256)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
