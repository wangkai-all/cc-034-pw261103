export type PackageState =
  | "detected"
  | "waiting_stable"
  | "receiving"
  | "received"
  | "validating"
  | "extracting"
  | "verifying"
  | "publishing"
  | "published"
  | "linked"
  | "failed"
  | "conflict"
  | "discarded";

export type BatchState =
  | "publishing"
  | "published"
  | "conflict"
  | "failed"
  | "discarded"
  | "superseded";

export interface CheckSummary {
  total: number;
  ok: number;
  failed: number;
}

export interface PackageProgress {
  stage?: string;
  percent?: number;
  current_file?: string;
}

export interface SourceObject {
  name: string;
  kind: "file" | "dir" | "symlink";
  bytes: number;
  mtime: number;
}

export interface PackageSummary {
  id: number;
  key: string;
  relative_path: string;
  package_code: string | null;
  state: PackageState;
  state_label: string;
  failure_code: string | null;
  reason: string | null;
  zip_name: string | null;
  zip_sha256: string | null;
  zip_bytes: number;
  member_count: number;
  total_uncompressed: number;
  batch_id: number | null;
  progress: PackageProgress;
  objects: SourceObject[];
  checks: CheckSummary;
  first_seen_at: string;
  updated_at: string;
}

export interface ManifestRow {
  rel_path: string;
  expected_bytes: number;
  expected_sha256: string;
  actual_bytes: number | null;
  actual_sha256: string | null;
  result: string;
  reason: string | null;
  sort_order: number;
}

export interface BatchSummary {
  id: number;
  package_code: string;
  state: BatchState;
  state_label: string;
  version: number;
  file_count: number;
  total_bytes: number;
  zip_name: string | null;
  zip_sha256: string;
  created_at: string;
  updated_at: string;
  publish_path: string | null;
  publish_reason: string | null;
}

export interface BatchFile {
  id: number;
  rel_path: string;
  byte_size: number;
  sha256: string;
  result: string;
  reason: string | null;
  sort_order: number;
}

export interface BatchDetail extends BatchSummary {
  files: BatchFile[];
  zip_download_url: string | null;
}

export interface PackageDetail extends PackageSummary {
  source_dir: string;
  entries: ManifestRow[];
  batch: BatchSummary | null;
}

export interface TaskProgress {
  scanning?: { status: string; message: string | null; candidates?: number };
  current_package?: {
    id?: number;
    folder?: string;
    stage?: string;
    percent?: number;
    current_file?: string;
  } | null;
  last_scan_at?: string;
  scanned_at?: string;
  package_counts?: Record<string, number>;
}

export interface TaskSummary {
  id: number;
  source_dir: string;
  status: "running" | "paused" | "pausing";
  created_at: string;
  finished_at: string | null;
  last_scan_at: string | null;
  scan_error: string | null;
  progress: TaskProgress;
  package_counts: Record<string, number>;
  batch_counts: Record<string, number>;
  limits: {
    single_file_bytes: number;
    single_file_label: string;
    total_expanded_bytes: number;
    total_expanded_label: string;
    max_files: number;
    scan_interval_seconds: number;
  };
  publish_dir: string;
}

export interface ConflictItem extends PackageSummary {
  incoming_batch: BatchSummary | null;
  existing_batch: BatchSummary | null;
}

export interface EventRecord {
  id: number;
  level: "info" | "warn" | "error";
  code: string;
  message: string;
  detail: unknown;
  created_at: string;
  package_id: number | null;
  batch_id: number | null;
}

export interface PreviewText {
  kind: "text";
  content: string;
  size: number;
  truncated: boolean;
  encoding: string;
}

export interface PreviewBinary {
  kind: "binary";
  message: string;
  size: number;
  mime?: string;
}

export type Preview = PreviewText | PreviewBinary;

export interface AppConfig {
  publish_dir: string;
  staging_dir: string;
  limits: {
    single_file_bytes: number;
    single_file_label: string;
    total_expanded_bytes: number;
    total_expanded_label: string;
    max_files: number;
    manifest_bytes: number;
    scan_interval_seconds: number;
  };
  preview_limit_bytes: number;
}
