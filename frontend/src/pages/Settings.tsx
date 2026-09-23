import { api, formatBytes } from "../api";
import { Banner, Card, Stat, useLoad } from "../components/ui";

const PATH_LABELS: Record<string, [string, string]> = {
  data: ["Application data", "LAYERSMITH_DATA_DIR"],
  images: ["Image exports", "LAYERSMITH_IMAGE_DIR"],
  builds: ["Build workspace", "LAYERSMITH_BUILD_DIR"],
  uploads: ["Uploaded files", "LAYERSMITH_UPLOAD_DIR"],
  logs: ["Build logs", "LAYERSMITH_LOG_DIR"],
  tmp: ["Temporary", "LAYERSMITH_TMP_DIR"],
};

export default function SettingsPage() {
  const { data, error, loading } = useLoad(() => api.settings());

  if (error) return <Banner>{error}</Banner>;
  if (loading && !data) return <p className="dim">Loading…</p>;
  if (!data) return null;

  return (
    <>
      <h1>Settings</h1>

      <Card title="General">
        <table className="table">
          <tbody>
            <tr><td data-label="Application">Application</td><td>{data.app_name} {data.version}</td></tr>
            <tr><td data-label="Default architecture">Default architecture</td><td className="mono">{data.default_architecture}</td></tr>
            <tr><td data-label="Default namespace">Default namespace</td><td className="mono">{data.default_namespace}</td></tr>
          </tbody>
        </table>
      </Card>

      <Card title="Build backend">
        {data.build_backend.available ? (
          <Banner kind="info">
            Building with <strong>{data.build_backend.name}</strong> ({data.build_backend.detail}), exporting a{" "}
            {data.build_backend.archive_format}.
          </Banner>
        ) : (
          <Banner>
            No usable container runtime, so builds will fail: {data.build_backend.detail}
          </Banner>
        )}
        <table className="table">
          <thead>
            <tr>
              <th>Runtime</th>
              <th>Status</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {data.build_backend.runtimes.map((runtime) => (
              <tr key={runtime.name}>
                <td data-label="Runtime">
                  <span className="mono">{runtime.name}</span>
                  {runtime.name === data.build_backend.name && (
                    <span className="pill ready" style={{ marginLeft: 8 }}>in use</span>
                  )}
                </td>
                <td data-label="Status">{runtime.available ? "available" : "not available"}</td>
                <td data-label="Detail" className="faint">{runtime.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="faint">
          <span className="mono">LAYERSMITH_BUILD_BACKEND</span> is{" "}
          <span className="mono">{data.build_backend.selection}</span>: <span className="mono">auto</span> prefers
          Podman and falls back to Docker, or name one explicitly. See the deployment documentation for running the
          builder on a separate host.
        </p>
      </Card>

      <Card title="Storage">
        <div className="grid stats">
          <Stat value={formatBytes(data.storage.total)} label="Disk" />
          <Stat value={formatBytes(data.storage.used)} label="Used" />
          <Stat value={formatBytes(data.storage.free)} label="Free" />
        </div>
        <table className="table" style={{ marginTop: "1rem" }}>
          <thead>
            <tr>
              <th>Purpose</th>
              <th>Path</th>
              <th>Environment variable</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(data.paths).map(([key, path]) => (
              <tr key={key}>
                <td data-label="Purpose">{PATH_LABELS[key]?.[0] ?? key}</td>
                <td data-label="Path" className="mono">{path}</td>
                <td data-label="Variable" className="mono faint">{PATH_LABELS[key]?.[1] ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="faint">
          Paths are set at startup and are created if missing. Nothing is deleted automatically.
        </p>
      </Card>
    </>
  );
}
