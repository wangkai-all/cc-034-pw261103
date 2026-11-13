import { useCallback, useEffect, useState } from "react";
import { api, formatTime } from "./api";
import { usePolling } from "./hooks/usePolling";
import { BatchList } from "./components/BatchList";
import { BatchPanel } from "./components/BatchPanel";
import { ConflictPanel } from "./components/ConflictPanel";
import { EventLog } from "./components/EventLog";
import { NewTaskForm } from "./components/NewTaskForm";
import { PackageDetailPanel } from "./components/PackageDetailPanel";
import { PackageTable } from "./components/PackageTable";
import { TaskList } from "./components/TaskList";
import { TaskOverview } from "./components/TaskOverview";
import type { AppConfig, TaskSummary } from "./types";

type Tab = "packages" | "batches" | "conflicts" | "events";

const TABS: { key: Tab; label: string }[] = [
  { key: "packages", label: "交付包" },
  { key: "batches", label: "批次与下载" },
  { key: "conflicts", label: "冲突" },
  { key: "events", label: "事件日志" },
];

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("packages");
  const [selectedPackageId, setSelectedPackageId] = useState<number | null>(null);
  const [openBatchId, setOpenBatchId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    api
      .config()
      .then(setConfig)
      .catch((err) =>
        setConfigError(err instanceof Error ? err.message : String(err)),
      );
  }, []);

  const tasksPoll = usePolling(() => api.listTasks(), 2500);
  const taskPoll = usePolling(
    () =>
      activeTaskId === null
        ? Promise.resolve(null)
        : api.getTask(activeTaskId),
    1500,
    [activeTaskId],
  );
  const packagesPoll = usePolling(
    () =>
      activeTaskId === null
        ? Promise.resolve([])
        : api.listPackages(activeTaskId),
    1500,
    [activeTaskId],
  );
  const batchesPoll = usePolling(
    () =>
      activeTaskId === null ? Promise.resolve([]) : api.listBatches(activeTaskId),
    2500,
    [activeTaskId],
  );
  const conflictsPoll = usePolling(
    () =>
      activeTaskId === null
        ? Promise.resolve([])
        : api.listConflicts(activeTaskId),
    2500,
    [activeTaskId],
  );
  const eventsPoll = usePolling(
    () =>
      activeTaskId === null ? Promise.resolve([]) : api.listEvents(activeTaskId),
    3000,
    [activeTaskId],
  );

  const tasks = tasksPoll.data ?? [];
  const task: TaskSummary | null =
    (taskPoll.data as TaskSummary | null) ??
    tasks.find((item) => item.id === activeTaskId) ??
    null;
  const packages = packagesPoll.data ?? [];
  const batches = batchesPoll.data ?? [];
  const conflicts = conflictsPoll.data ?? [];

  useEffect(() => {
    if (activeTaskId === null && tasks.length > 0) {
      setActiveTaskId(tasks[0].id);
    }
  }, [tasks, activeTaskId]);

  useEffect(() => {
    setSelectedPackageId(null);
    setOpenBatchId(null);
    setTab("packages");
  }, [activeTaskId]);

  const handleAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setActionError(null);
      try {
        await fn();
        tasksPoll.refresh();
        taskPoll.refresh();
        packagesPoll.refresh();
        batchesPoll.refresh();
        conflictsPoll.refresh();
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [tasksPoll, taskPoll, packagesPoll, batchesPoll, conflictsPoll],
  );

  const selectedSummary =
    packages.find((pkg) => pkg.id === selectedPackageId) ?? null;
  const conflictCount = conflicts.length;

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <h1>交付包接收台</h1>
          <span>本地目录 · 自动发现 · 校验后发布</span>
        </div>
        <div className="header-spacer" />
        {config && (
          <div className="header-meta">
            <span>
              发布目录 <b>{config.publish_dir}</b>
            </span>
            <span>
              单文件 <b>{config.limits.single_file_label}</b>
            </span>
            <span>
              总展开 <b>{config.limits.total_expanded_label}</b>
            </span>
          </div>
        )}
      </header>

      {configError && (
        <div style={{ padding: "10px 28px 0" }}>
          <div className="notice error">
            无法连接后端接口：{configError}。请确认 FastAPI 服务已启动。
          </div>
        </div>
      )}

      <div className="layout">
        <aside className="sidebar">
          <NewTaskForm
            publishDir={config?.publish_dir ?? "(未加载)"}
            onCreated={(created) => {
              setActiveTaskId(created.id);
              tasksPoll.refresh();
            }}
          />
          <TaskList
            tasks={tasks}
            activeId={activeTaskId}
            onSelect={(selected) => setActiveTaskId(selected.id)}
          />
        </aside>

        <main className="main-area">
          {task === null ? (
            <section className="panel">
              <div className="empty-state">
                <h3>开始前先指定一个服务端本地目录</h3>
                输入绝对路径后，应用会自动发现子目录形式的交付包，并显示扫描与接收进度。
              </div>
            </section>
          ) : (
            <>
              {(actionError || taskPoll.error) && (
                <div className="notice error">
                  操作失败：{actionError ?? taskPoll.error}
                </div>
              )}
              <TaskOverview
                task={task}
                busy={busy}
                onPause={() =>
                  handleAction(() => api.pauseTask(task.id))
                }
                onResume={() =>
                  handleAction(() => api.resumeTask(task.id))
                }
                onRescan={() => handleAction(() => api.rescan(task.id))}
              />

              <section className="panel tab-panel">
                <div className="panel-head">
                  <div className="segmented">
                    {TABS.map((item) => (
                      <button
                        key={item.key}
                        className={tab === item.key ? "active" : ""}
                        onClick={() => setTab(item.key)}
                      >
                        {item.label}
                        {item.key === "conflicts" && conflictCount > 0
                          ? ` (${conflictCount})`
                          : ""}
                      </button>
                    ))}
                  </div>
                  <div style={{ flex: 1 }} />
                  {packagesPoll.error && (
                    <span className="hint" style={{ color: "var(--error)" }}>
                      刷新失败：{packagesPoll.error}
                    </span>
                  )}
                  <span className="hint">更新于 {formatTime(task.last_scan_at)}</span>
                </div>

                {tab === "packages" && (
                  <>
                    <PackageTable
                      packages={packages}
                      selectedId={selectedPackageId}
                      onSelect={(pkg) => setSelectedPackageId(pkg.id)}
                    />
                    <div style={{ borderTop: "1px solid var(--border)" }}>
                      <PackageDetailPanel
                        packageId={selectedPackageId}
                        summary={selectedSummary}
                        onOpenBatch={(id) => {
                          setOpenBatchId(id);
                          setTab("batches");
                        }}
                        onConflict={() => setTab("conflicts")}
                      />
                    </div>
                  </>
                )}

                {tab === "batches" && (
                  <>
                    <BatchList
                      batches={batches}
                      activeId={openBatchId}
                      onOpen={(id) =>
                        setOpenBatchId((current) => (current === id ? null : id))
                      }
                    />
                    <div style={{ borderTop: "1px solid var(--border)" }}>
                      <BatchPanel
                        batchId={openBatchId}
                        onClose={() => setOpenBatchId(null)}
                      />
                    </div>
                  </>
                )}

                {tab === "conflicts" && (
                  <ConflictPanel
                    conflicts={conflicts}
                    onResolved={() => {
                      packagesPoll.refresh();
                      conflictsPoll.refresh();
                      batchesPoll.refresh();
                      tasksPoll.refresh();
                    }}
                  />
                )}

                {tab === "events" && (
                  <EventLog events={eventsPoll.data ?? []} />
                )}
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
