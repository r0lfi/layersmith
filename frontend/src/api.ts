/** Thin client for the LayerSmith HTTP API. */

export type BuildStatus =
  | "queued" | "preparing" | "pulling" | "building" | "testing" | "exporting"
  | "ready" | "failed" | "cancelled";

export const ACTIVE: BuildStatus[] = ["queued", "preparing", "pulling", "building", "testing", "exporting"];

export interface Build {
  id: string;
  number: number;
  project_id: string;
  project_name?: string | null;
  version: string;
  status: BuildStatus;
  reason: string;
  error?: string | null;
  mode: string;
  architecture: string;
  base_image?: string | null;
  base_digest?: string | null;
  packages: string[];
  image_ref?: string | null;
  image_id?: string | null;
  image_digest?: string | null;
  image_size?: number | null;
  export_size?: number | null;
  export_sha256?: string | null;
  has_export: boolean;
  has_airgap: boolean;
  airgap_size?: number | null;
  airgap_sha256?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  duration_seconds?: number | null;
  containerfile?: string;
  log?: string;
  manifest?: Record<string, unknown>;
  training_profile?: string | null;
  training?: TrainingDetail | null;
}

/* ------------------------------------------------- LLM training profiles */

export interface TrainingRequest {
  profile?: string;
  target?: string;
  stack?: string;
  addons?: string[];
  intent?: Record<string, string>;
  extra_python?: string[];
}

export interface TrainingTool {
  id: string;
  name: string;
  package: string;
  what: string;
  used_for: string;
  when: string;
  pulls_in: string;
  limitations: string;
  docs: string;
  version?: string | null;
  required: boolean;
  why: string;
  addon?: string | null;
}

export interface TrainingRecipe {
  profile: string;
  profile_name: string;
  profile_version: string;
  target: string;
  target_name: string;
  gpu: boolean;
  architecture: string;
  stack: string;
  stack_name: string;
  stack_summary: string;
  addons: string[];
  intent: Record<string, string>;
  base: { image: string; tag: string; digest: string; reference: string; display: string; reason: string };
  python: string;
  torch: string | null;
  cuda: string | null;
  driver: string | null;
  gpu_support: string | null;
  host_requirements: string[];
  versions: Record<string, string>;
  lock_digest: string;
  compiled: string[];
  tools: TrainingTool[];
  system_packages: string[];
  system_why: string;
  extra_python: string[];
  ports: { port: number; name: string }[];
  notes: string[];
}

export interface TrainingProfile {
  id: string;
  name: string;
  recommended: boolean;
  summary: string;
  description: string;
  best_for: string[];
  targets: string[];
  stacks: string[];
  addons: string[];
  default_addons: string[];
  intent: Record<string, string>;
  fits: Record<string, string[]>;
}

export interface TrainingCatalog {
  category: { id: string; name: string; intro: string; before_build: string };
  concepts: { id: string; term: string; text: string }[];
  intents: Record<string, { label: string; hint: string; options: { id: string; label: string }[] }>;
  targets: Record<string, { name: string; architecture: string; gpu: boolean; summary: string; host_requirements: string[] }>;
  stacks: Record<string, { name: string; summary: string; target: string; python: string; torch: string | null;
    cuda: string | null; driver: string | null; gpu_support: string | null; versions: Record<string, string> }>;
  addons: Record<string, { name: string; tools: string[]; summary: string; stacks?: string[]; needs_devel_base?: boolean }>;
  tools: Record<string, Omit<TrainingTool, "id" | "version" | "required" | "why" | "addon">>;
  profiles: TrainingProfile[];
  directories: { path: string; host: string; mode: string; purpose: string }[];
}

export interface ExportContents {
  summary: string;
  included: string[];
  external: string[];
}

export interface GuideStep {
  title: string;
  text?: string;
  code?: string;
  docker?: string;
  podman?: string;
}

export interface GuideSection extends GuideStep {
  id: string;
  note?: string;
  link?: string;
  steps?: GuideStep[];
  table?: { path: string; host: string; mode: string; purpose: string }[];
}

export interface TrainingResolve {
  ok: boolean;
  error?: string;
  alternative?: TrainingRequest & { architecture?: string } | null;
  label?: string | null;
  recipe?: TrainingRecipe;
  recommendation?: { profile: string; reason: string };
  export_contents?: ExportContents;
  before_build?: string;
}

export type CheckStatus = "passed" | "failed" | "not_run";

export interface CheckRow {
  id: "image" | "deps" | "cpu" | "gpu" | "offline";
  status: CheckStatus;
  label: string;
  meaning: string;
  summary: string;
  steps?: { name: string; status: string; detail: string; seconds?: number }[];
  info?: Record<string, unknown>;
  finished?: string | null;
  command?: { docker: string; podman: string } | null;
  applies_to: string;
}

export interface TrainingDetail {
  recipe: TrainingRecipe;
  customized: boolean;
  checks: CheckRow[];
  getting_started: { image: string; sections: GuideSection[] };
  export_contents: ExportContents;
  before_build: string;
}

export type ScanState = "queued" | "exporting" | "scanning" | "completed" | "failed";
export type Severity = "critical" | "high" | "medium" | "low" | "unknown";

export const SEVERITIES: Severity[] = ["critical", "high", "medium", "low", "unknown"];
export const SCAN_ACTIVE: ScanState[] = ["queued", "exporting", "scanning"];

export interface Scan {
  id: string;
  build_id: string;
  image_digest?: string | null;
  state: ScanState;
  reason: string;
  error?: string | null;
  kinds: string[];
  scanner: string;
  scanner_version: string;
  database: { version?: string | null; updated_at?: string | null; offline: boolean };
  /** Whether the scan reused the stored export or made a temporary one. */
  archive_source?: string | null;
  /** Set when the scanner named an image identity LayerSmith did not expect. */
  identity_note?: string | null;
  counts: Record<string, Record<string, number>>;
  severity: Record<Severity, number>;
  total: number;
  sbom?: { format: string; components: number | null; sha256: string | null } | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  created_at?: string | null;
  findings?: Finding[];
  /** How many findings matched, before the response was capped. */
  finding_total?: number;
  finding_limit?: number;
}

export interface Finding {
  id: string;
  kind: "vulnerability" | "secret" | "misconfiguration";
  severity: Severity;
  identifier: string;
  title: string;
  target: string;
  package_name?: string | null;
  installed_version?: string | null;
  fixed_version?: string | null;
  url?: string | null;
  /** A description of a secret match. Never the secret itself. */
  masked_match?: string | null;
}

export interface Project {
  id: string;
  name: string;
  description: string;
  repository: string;
  template: string;
  mode: "gui" | "advanced";
  spec: Spec;
  containerfile: string;
  next_version: string;
  base_image?: string | null;
  base_digest?: string | null;
  base_update_available: boolean;
  build_count?: number;
  created_at?: string;
  updated_at?: string;
  builds?: Build[];
  packages?: string[];
  warnings?: string[];
  training?: TrainingRecipe | null;
}

export interface Spec {
  base?: { distribution?: string; version?: string; source?: string; family?: string };
  architecture?: string;
  presets?: string[];
  packages?: string[];
  extra_packages?: string[];
  files?: { sha256: string; destination: string; mode?: string; filename?: string }[];
  scripts?: Record<string, string>;
  env?: { name: string; value: string }[];
  tests?: string[];
  enable_epel?: boolean;
  workdir?: string;
  tag_latest?: boolean;
  training?: TrainingRequest;
}

export interface Catalog {
  distributions: { name: string; family: string; el: boolean; versions: string[]; sources: Record<string, string> }[];
  families: Record<string, string>;
  architectures: string[];
  templates: { name: string; description: string; spec: Spec }[];
  presets: { name: string; description: string; packages: string[]; tools?: string[] }[];
  categories: { name: string; packages: string[] }[];
  tools: Record<string, { install_path: string; default_version: string }>;
}

export interface Settings {
  app_name: string;
  version: string;
  revision: string;
  tagline: string;
  source_url: string;
  license: string;
  default_architecture: string;
  default_namespace: string;
  build_backend: {
    name: string;
    selection: string;
    available: boolean;
    detail: string;
    archive_format: string;
    runtimes: { name: string; available: boolean; detail: string }[];
  };
  scanner: {
    name: string;
    selection: string;
    available: boolean;
    detail: string;
    version: string;
    supported_kinds: string[];
    supports_sbom: boolean;
    scan_after_build: boolean;
    database: { version: string | null; updated_at: string | null; offline: boolean; detail: string };
    scanners: { name: string; available: boolean; detail: string }[];
  };
  paths: {
    field: string;
    label: string;
    path: string;
    variable: string;
    editable: boolean;
    source: "default" | "environment" | "setting";
    note: string;
  }[];
  storage: { total: number; used: number; free: number };
}

export interface Stats {
  projects: number;
  builds: number;
  images: number;
  storage_bytes: number;
  recent: Build[];
}

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init?.headers : { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: any) => d.msg).join("; ");
    } catch {
      /* keep the status line */
    }
    throw new ApiError(detail);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export const api = {
  catalog: () => request<Catalog>("/catalog"),
  settings: () => request<Settings>("/settings"),
  updateStorage: (paths: Record<string, string>) =>
    request<{ paths: Settings["paths"] }>("/settings/storage", { method: "PUT", body: JSON.stringify({ paths }) }),
  stats: () => request<Stats>("/stats"),

  projects: () => request<Project[]>("/projects"),
  project: (id: string) => request<Project>(`/projects/${id}`),
  createProject: (body: unknown) => request<Project>("/projects", { method: "POST", body: JSON.stringify(body) }),
  importProject: (body: unknown) =>
    request<Project>("/projects/import", { method: "POST", body: JSON.stringify(body) }),
  updateProject: (id: string, body: unknown) =>
    request<Project>(`/projects/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  cloneProject: (id: string, name: string) =>
    request<Project>(`/projects/${id}/clone`, { method: "POST", body: JSON.stringify({ name }) }),
  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),

  preview: (body: unknown) =>
    request<{ containerfile: string; packages: string[]; warnings: string[] }>("/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  builds: (limit = 50) => request<Build[]>(`/builds?limit=${limit}`),
  build: (id: string) => request<Build>(`/builds/${id}`),
  startBuild: (projectId: string, version?: string) =>
    request<Build>(`/projects/${projectId}/builds`, {
      method: "POST",
      body: JSON.stringify(version ? { version } : {}),
    }),
  airgap: (buildId: string) =>
    request<{ filename: string; size: number; sha256: string }>(`/builds/${buildId}/airgap`, { method: "POST" }),
  downloadUrl: (buildId: string, kind: "export" | "airgap") => `/api/builds/${buildId}/download/${kind}`,

  scans: (buildId: string) => request<Scan[]>(`/builds/${buildId}/scans`),
  startScan: (buildId: string) => request<Scan>(`/builds/${buildId}/scan`, { method: "POST", body: "{}" }),
  scan: (scanId: string, filters: { kind?: string; severity?: string } = {}) => {
    const query = new URLSearchParams();
    if (filters.kind) query.set("kind", filters.kind);
    if (filters.severity) query.set("severity", filters.severity);
    const suffix = query.toString();
    return request<Scan>(`/scans/${scanId}${suffix ? `?${suffix}` : ""}`);
  },
  sbomUrl: (scanId: string) => `/api/scans/${scanId}/sbom`,

  trainingCatalog: () => request<TrainingCatalog>("/training/catalog"),
  trainingResolve: (training: TrainingRequest, architecture?: string) =>
    request<TrainingResolve>("/training/resolve", { method: "POST", body: JSON.stringify({ training, architecture }) }),
  gpuReport: (buildId: string, report: string) =>
    request<TrainingDetail>(`/builds/${buildId}/gpu-report`, { method: "POST", body: JSON.stringify({ report }) }),

  upload: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ sha256: string; filename: string; size: number }>("/uploads", { method: "POST", body: form });
  },
};

export function buildLogSocket(buildId: string): WebSocket {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${protocol}//${location.host}/api/builds/${buildId}/logs`);
}

export function formatBytes(bytes?: number | null): string {
  if (!bytes && bytes !== 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds - minutes * 60)}s`;
}

export function formatWhen(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  const diff = (Date.now() - date.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
