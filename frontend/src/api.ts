import type {
  AppConfig,
  BatchDetail,
  BatchSummary,
  ConflictItem,
  EventRecord,
  PackageDetail,
  PackageSummary,
  Preview,
  TaskSummary,
} from "./types";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (body.detail) message = JSON.stringify(body.detail);
    } catch {
      /* keep default message */
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  listTasks: () => request<TaskSummary[]>("/api/tasks"),
  getTask: (id: number) => request<TaskSummary>(`/api/tasks/${id}`),
  createTask: (directory: string, allowBroad: boolean) =>
    request<TaskSummary>("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        directory,
        allow_broad_directory: allowBroad,
      }),
    }),
  pauseTask: (id: number) =>
    request<TaskSummary>(`/api/tasks/${id}/pause`, { method: "POST" }),
  resumeTask: (id: number) =>
    request<TaskSummary>(`/api/tasks/${id}/resume`, { method: "POST" }),
  rescan: (id: number) =>
    request<TaskSummary>(`/api/tasks/${id}/rescan`, { method: "POST" }),
  scanNow: (id: number) =>
    request<{ ok: boolean; triggered: boolean }>(`/api/tasks/${id}/scan`, {
      method: "POST",
    }),
  listPackages: (taskId: number) =>
    request<PackageSummary[]>(`/api/tasks/${taskId}/packages`),
  getPackage: (id: number) => request<PackageDetail>(`/api/packages/${id}`),
  listBatches: (taskId: number) =>
    request<BatchSummary[]>(`/api/tasks/${taskId}/batches`),
  getBatch: (id: number) => request<BatchDetail>(`/api/batches/${id}`),
  listConflicts: (taskId: number) =>
    request<ConflictItem[]>(`/api/tasks/${taskId}/conflicts`),
  resolveConflict: (packageId: number, resolution: "keep_existing" | "receive_incoming") =>
    request<{ ok: boolean; resolution: string; batch_id: number }>(
      `/api/packages/${packageId}/resolve`,
      { method: "POST", body: JSON.stringify({ resolution }) },
    ),
  listEvents: (taskId: number, limit = 200) =>
    request<EventRecord[]>(`/api/tasks/${taskId}/events?limit=${limit}`),
  preview: (batchId: number, fileId: number) =>
    request<Preview>(`/api/batches/${batchId}/files/${fileId}/preview`),
  downloadUrl: (batchId: number, fileId: number) =>
    `/api/batches/${batchId}/files/${fileId}/download`,
  zipDownloadUrl: (batchId: number) => `/api/batches/${batchId}/zip`,
};

export { ApiError };

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(1)} ${units[unit]}`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function shortHash(hash: string | null | undefined): string {
  if (!hash) return "-";
  return `${hash.slice(0, 8)}…${hash.slice(-6)}`;
}
