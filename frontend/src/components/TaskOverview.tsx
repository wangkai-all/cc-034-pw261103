import { formatTime } from "../api";
import { stageLabel } from "../labels";
import type { TaskSummary } from "../types";

interface Props {
  task: TaskSummary;
  onPause: () => void;
  onResume: () => void;
  onRescan: () => void;
  busy: boolean;
}

export function TaskOverview({ task, onPause, onResume, onRescan, busy }: Props) {
  const running = task.status === "running";
  const scanning = task.progress.scanning;
  const current = task.progress.current_package;
  const counts = task.package_counts;

  return (
    <section className="panel task-hero">
      <div className="hero-top">
        <div style={{ minWidth: 0, flex: 1 }}>
          <h2>任务 #{task.id}</h2>
          <div className="hero-path">{task.source_dir}</div>
        </div>
        <div className="actions-row">
          <button className="ghost-btn" onClick={onRescan} disabled={busy}>
            立即扫描
          </button>
          {running ? (
            <button className="ghost-btn" onClick={onPause} disabled={busy}>
              暂停
            </button>
          ) : (
            <button className="primary-btn" style={{ padding: "6px 12px" }} onClick={onResume} disabled={busy}>
              继续
            </button>
          )}
        </div>
      </div>

      {task.scan_error && (
        <div className="notice error" style={{ marginTop: 12 }}>
          扫描出错：{task.scan_error}
        </div>
      )}
      {!task.scan_error && scanning?.status === "error" && scanning.message && (
        <div className="notice error" style={{ marginTop: 12 }}>
          {scanning.message}
        </div>
      )}

      <div className="hero-stats">
        <div className="stat">
          <div className="stat-label">任务状态</div>
          <div className="stat-value">{running ? "扫描运行中" : "已暂停"}</div>
          <div className="stat-foot">上次扫描 {formatTime(task.last_scan_at)}</div>
        </div>
        <div className="stat">
          <div className="stat-label">已发布批次</div>
          <div className="stat-value">{counts.published ?? 0}</div>
          <div className="stat-foot">
            关联 {(counts.linked ?? 0)} / 冲突 {(counts.conflict ?? 0)}
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">等待稳定</div>
          <div className="stat-value">{counts.waiting_stable ?? 0}</div>
          <div className="stat-foot">接收中 {(counts.receiving ?? 0) + (counts.validating ?? 0) + (counts.extracting ?? 0) + (counts.verifying ?? 0) + (counts.publishing ?? 0)}</div>
        </div>
        <div className="stat">
          <div className="stat-label">失败</div>
          <div className="stat-value" style={{ color: counts.failed ? "var(--error)" : undefined }}>
            {counts.failed ?? 0}
          </div>
          <div className="stat-foot">记录保留在任务详情中</div>
        </div>
      </div>

      {(current || scanning) && (
        <div className="progress-block">
          {current ? (
            <>
              <div className="progress-line">
                <span>
                  当前交付包：<b>{current.folder ?? `#${current.id}`}</b>
                  {current.stage && <> · {stageLabel(current.stage)}</>}
                  {current.current_file && (
                    <span className="mono faint"> · {current.current_file}</span>
                  )}
                </span>
                <b>{current.percent ?? 0}%</b>
              </div>
              <div className="bar">
                <span
                  style={{
                    width: `${Math.min(100, Math.max(0, current.percent ?? 0))}%`,
                    background:
                      current.stage === "publishing" ? "var(--ok)" : undefined,
                  }}
                />
              </div>
            </>
          ) : (
            <div className="progress-line">
              <span>
                {running
                  ? scanning?.status === "scanning"
                    ? "正在扫描目录…"
                    : "等待下一次扫描"
                  : "任务已暂停，不再扫描"}
              </span>
              <b>{formatTime(task.progress.last_scan_at ?? task.last_scan_at)}</b>
            </div>
          )}
          <div className="progress-line">
            <span className="hint">
              扫描间隔 {task.limits.scan_interval_seconds}s · 单文件上限{" "}
              {task.limits.single_file_label} · 总展开上限{" "}
              {task.limits.total_expanded_label} · 文件数上限 {task.limits.max_files}
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
