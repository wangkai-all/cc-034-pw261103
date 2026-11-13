import type { BatchState, PackageState } from "./types";

const STATE_TONE: Record<string, string> = {
  detected: "wait",
  waiting_stable: "wait",
  receiving: "busy",
  received: "wait",
  validating: "busy",
  extracting: "busy",
  verifying: "busy",
  publishing: "busy",
  published: "ok",
  linked: "ok",
  failed: "err",
  conflict: "warn",
  discarded: "err",
  superseded: "superseded",
  running: "busy",
  paused: "wait",
};

export function stateTone(state: PackageState | BatchState | string): string {
  return STATE_TONE[state] ?? "wait";
}

export const RESULT_LABELS: Record<string, string> = {
  ok: "校验通过",
  missing: "缺失",
  extra: "多余",
  size_mismatch: "字节数不符",
  hash_mismatch: "散列不符",
  not_checked: "未校验",
};

export function resultTone(result: string): string {
  if (result === "ok") return "ok";
  if (result === "not_checked") return "wait";
  return "err";
}

export const FAILURE_LABELS: Record<string, string> = {
  manifest_missing: "缺少清单",
  manifest_unreadable: "清单不可读",
  manifest_malformed: "清单不是合法 JSON",
  manifest_fields: "清单字段有误",
  manifest_entry: "清单条目有误",
  manifest_duplicate: "清单路径重复",
  manifest_too_large: "清单过大",
  reserved_path: "保留路径冲突",
  manifest_symlink: "清单是符号链接",
  manifest_changed: "清单仍在变化",
  path_invalid: "路径不安全",
  case_collision: "忽略大小写后重名",
  zip_missing: "缺少压缩包",
  zip_unreadable: "压缩包不可读",
  zip_symlink: "压缩包是符号链接",
  zip_corrupt: "压缩包损坏",
  zip_encrypted: "压缩包被加密",
  zip_duplicate: "压缩包条目重复",
  symlink: "包含符号链接",
  too_many_files: "文件数量超限",
  file_too_large: "单文件超限",
  expanded_too_large: "展开体积超限",
  size_mismatch: "字节数不符",
  manifest_mismatch: "压缩包与清单不符",
  zip_hash_mismatch: "压缩包散列与清单不符",
  content_mismatch: "文件内容校验失败",
  source_changed: "源文件仍在变化",
  code_conflict: "交付编号冲突",
  publish_error: "发布失败",
  internal_error: "内部错误",
  task_crashed: "任务异常",
  scan_crashed: "扫描异常",
};

export function failureLabel(code: string | null | undefined): string {
  if (!code) return "";
  return FAILURE_LABELS[code] ?? code;
}

export const STAGE_LABELS: Record<string, string> = {
  detected: "已发现",
  waiting_stable: "等待文件稳定",
  receiving: "接收中",
  received: "已接收",
  validating: "检查压缩包",
  extracting: "解压与校验",
  verifying: "逐文件核对",
  publishing: "发布中",
  published: "已发布",
  linked: "已关联",
  failed: "失败",
  conflict: "冲突",
};

export function stageLabel(stage: string | undefined): string {
  if (!stage) return "";
  return STAGE_LABELS[stage] ?? stage;
}
