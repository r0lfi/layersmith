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
  workdir?: string;
  tag_latest?: boolean;
}

export interface Catalog {
  distributions: { name: string; family: string; versions: string[]; sources: Record<string, string> }[];
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
  tagline: string;
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
