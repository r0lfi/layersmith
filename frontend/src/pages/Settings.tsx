import { useEffect, useState } from "react";
import { api, formatBytes } from "../api";
import { Banner, Card, Stat, useLoad } from "../components/ui";

export default function SettingsPage() {
  const { data, error, loading, reload } = useLoad(() => api.settings());
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (data) setEdits(Object.fromEntries(data.paths.map((row) => [row.field, row.path])));
  }, [data]);

  if (error) return <Banner>{error}</Banner>;
  if (loading && !data) return <p className="dim">Loading…</p>;
  if (!data) return null;

  const changed = data.paths.filter((row) => row.editable && edits[row.field] && edits[row.field] !== row.path);

  async function save() {
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      await api.updateStorage(Object.fromEntries(changed.map((row) => [row.field, edits[row.field]])));
      setSaved(true);
      reload();
    } catch (exc) {
      setSaveError((exc as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <h1>Settings</h1>

      <Card title="About">
        <div className="row" style={{ alignItems: "flex-start", gap: "1rem" }}>
          <img src="/logo-mark.svg" alt="" width={48} height={48} />
          <div>
            <h2 style={{ margin: 0 }}>
              {data.app_name} <span className="faint">{data.version}</span>
            </h2>
            <p className="dim" style={{ margin: "0.1rem 0" }}>{data.tagline}</p>
            <p className="faint" style={{ margin: 0 }}>by xnett.org</p>
          </div>
        </div>
        <table className="table" style={{ marginTop: "0.8rem" }}>
          <tbody>
            <tr><td data-label="Version">Version</td><td className="mono">{data.version}</td></tr>
            <tr>
              <td data-label="Revision">Revision</td>
              <td className="mono truncate" title={data.revision}>{data.revision}</td>
            </tr>
            <tr><td data-label="License">License</td><td>{data.license}</td></tr>
            <tr>
              <td data-label="Source">Source</td>
              <td>
                <a href={data.source_url} target="_blank" rel="noreferrer noopener">{data.source_url}</a>
              </td>
            </tr>
          </tbody>
        </table>
      </Card>

      <Card title="Defaults">
        <table className="table">
          <tbody>
            <tr>
              <td data-label="Default architecture">Default architecture</td>
              <td className="mono">{data.default_architecture}</td>
            </tr>
            <tr>
              <td data-label="Default namespace">Default namespace</td>
              <td className="mono">{data.default_namespace}</td>
            </tr>
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
          <Banner>No usable container runtime, so builds will fail: {data.build_backend.detail}</Banner>
        )}
        <table className="table">
          <thead>
            <tr><th>Runtime</th><th>Status</th><th>Detail</th></tr>
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
          Podman and falls back to Docker, or name one explicitly.
        </p>
      </Card>

      <Card title="Security">
        {data.scanner.available ? (
          <Banner kind="info">
            Scanning with <strong>{data.scanner.name}</strong>
            {data.scanner.version && <> {data.scanner.version}</>}.{" "}
            {data.scanner.scan_after_build
              ? "Images are scanned automatically after every build."
              : "Automatic scanning after a build is switched off."}
          </Banner>
        ) : (
          <Banner>
            Images are not scanned. Nothing is known about their vulnerabilities — an unscanned image is
            unknown, not clean. {data.scanner.detail}
          </Banner>
        )}
        <table className="table">
          <thead>
            <tr><th>Scanner</th><th>Status</th><th>Detail</th></tr>
          </thead>
          <tbody>
            {data.scanner.scanners.map((scanner) => (
              <tr key={scanner.name}>
                <td data-label="Scanner">
                  <span className="mono">{scanner.name}</span>
                  {scanner.name === data.scanner.name && (
                    <span className="pill ready" style={{ marginLeft: 8 }}>in use</span>
                  )}
                </td>
                <td data-label="Status">{scanner.available ? "available" : "not available"}</td>
                <td data-label="Detail" className="faint">{scanner.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.scanner.available && data.scanner.database.detail && (
          <p className="faint">
            Vulnerability data: {data.scanner.database.detail}
            {data.scanner.database.offline && " (imported offline)"}
          </p>
        )}
        <p className="faint">
          <span className="mono">LAYERSMITH_SCANNER</span> is{" "}
          <span className="mono">{data.scanner.selection}</span>: <span className="mono">auto</span> uses a scanner
          when one is reachable. Scanning reports; it never blocks a build.
        </p>
      </Card>

      <Card title="Storage">
        <div className="grid stats">
          <Stat value={formatBytes(data.storage.total)} label="Disk" />
          <Stat value={formatBytes(data.storage.used)} label="Used" />
          <Stat value={formatBytes(data.storage.free)} label="Free" />
        </div>

        {saveError && <Banner>{saveError}</Banner>}
        {saved && changed.length === 0 && <Banner kind="info">Saved. New data is written to the new paths.</Banner>}

        <p className="faint" style={{ marginTop: "1rem" }}>
          Where new data is written. Changing a path does not move files that already exist, and archives written
          earlier stay downloadable.
        </p>

        {data.paths.map((row) => (
          <div className="field" key={row.field}>
            <label htmlFor={row.field}>
              {row.label}{" "}
              <span className="faint mono">{row.variable}</span>
              {row.source === "environment" && <span className="pill queued" style={{ marginLeft: 8 }}>from environment</span>}
              {row.source === "setting" && <span className="pill running" style={{ marginLeft: 8 }}>changed</span>}
            </label>
            <input
              id={row.field}
              className="mono"
              value={edits[row.field] ?? row.path}
              disabled={!row.editable}
              onChange={(event) => setEdits({ ...edits, [row.field]: event.target.value })}
            />
            {row.note && <div className="field-hint">{row.note}</div>}
          </div>
        ))}

        <div className="row end">
          {changed.length > 0 && (
            <button
              className="ghost"
              onClick={() => setEdits(Object.fromEntries(data.paths.map((r) => [r.field, r.path])))}
            >
              Reset
            </button>
          )}
          <button className="primary" onClick={save} disabled={saving || changed.length === 0}>
            {saving ? "Saving…" : changed.length ? `Save ${changed.length} path${changed.length > 1 ? "s" : ""}` : "Save"}
          </button>
        </div>
      </Card>
    </>
  );
}
