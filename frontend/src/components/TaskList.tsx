import { formatTime } from "../api";
import { StatusPill } from "./StatusPill";
import type { TaskSummary } from "../types";

interface Props {
  tasks: TaskSummary[];
  activeId: number | null;
  onSelect: (task: TaskSummary) => void;
}

export function TaskList({ tasks, activeId, onSelect }: Props) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>任务</h2>
        <span className="badge-count">{tasks.length}</span>
      </div>
      <div className="task-list">
        {tasks.length === 0 && (
          <div className="empty-state">还没有任务。输入目录路径开始扫描。</div>
        )}
        {tasks.map((task) => {
          const failed = (task.package_counts.failed ?? 0) + (task.package_counts.conflict ?? 0);
          return (
            <button
              key={task.id}
              className={`task-item ${task.id === activeId ? "active" : ""}`}
              onClick={() => onSelect(task)}
              type="button"
            >
              <strong title={task.source_dir}>#{task.id} {task.source_dir}</strong>
              <div className="task-sub">
                <StatusPill
                  state={task.status}
                  label={task.status === "running" ? "扫描中" : "已暂停"}
                />
                <span>已发布 {task.package_counts.published ?? 0}</span>
                {failed > 0 && <span style={{ color: "var(--error)" }}>异常 {failed}</span>}
                <span className="muted">{formatTime(task.last_scan_at)}</span>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}
