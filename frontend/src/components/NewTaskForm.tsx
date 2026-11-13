import { useState } from "react";
import { ApiError, api } from "../api";
import type { TaskSummary } from "../types";

interface Props {
  onCreated: (task: TaskSummary) => void;
  publishDir: string;
}

export function NewTaskForm({ onCreated, publishDir }: Props) {
  const [directory, setDirectory] = useState("");
  const [allowBroad, setAllowBroad] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!directory.trim()) {
      setError("请输入要扫描的绝对目录路径。");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const task = await api.createTask(directory.trim(), allowBroad);
      setDirectory("");
      setNotice(`任务 #${task.id} 已启动，正在扫描 ${task.source_dir}`);
      onCreated(task);
    } catch (err) {
      if (err instanceof ApiError) setError(err.message);
      else setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>新建接收任务</h2>
      </div>
      <form className="panel-body form-stack" onSubmit={submit}>
        <div className="field">
          <label htmlFor="source-dir">服务端本地目录（绝对路径）</label>
          <input
            id="source-dir"
            className="text-input"
            placeholder="/srv/deliveries/incoming"
            value={directory}
            onChange={(event) => setDirectory(event.target.value)}
            spellCheck={false}
            autoComplete="off"
          />
        </div>
        <div className="checkbox-row">
          <input
            id="allow-broad"
            type="checkbox"
            checked={allowBroad}
            onChange={(event) => setAllowBroad(event.target.checked)}
          />
          <label htmlFor="allow-broad">
            允许扫描范围较宽的目录（仅当目录确实只存放交付包时勾选）
          </label>
        </div>
        <button className="primary-btn" type="submit" disabled={busy}>
          {busy ? <><span className="spinner" /> 启动中</> : "开始扫描"}
        </button>
        {error && <div className="notice error">{error}</div>}
        {notice && <div className="notice ok">{notice}</div>}
        <p className="hint">
          每个交付包必须是一个子目录，内含 <span className="mono">manifest.json</span> 与一个 ZIP。
          文件大小连续两次扫描不变后才会被接收。发布目录：<span className="mono">{publishDir}</span>
        </p>
      </form>
    </section>
  );
}
