import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, formatWhen } from "../api";
import { Banner, Card, Empty, Field, useLoad } from "../components/ui";

export default function Projects() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { data, error, loading, reload } = useLoad(() => api.projects());
  // The dashboard and the wizard link here with ?import=1 to open the form.
  const [importing, setImporting] = useState(params.get("import") === "1");
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [importError, setImportError] = useState<string | null>(null);

  async function runImport() {
    setImportError(null);
    try {
      const project = await api.importProject({ name, containerfile: text });
      navigate(`/projects/${project.id}`);
    } catch (exc) {
      setImportError((exc as Error).message);
    }
  }

  return (
    <>
      <div className="row">
        <div>
          <h1>Images</h1>
          <p className="dim" style={{ margin: 0 }}>
            Each image keeps its definition, its versions and its build history. Build from a template, or
            import a Dockerfile you already have.
          </p>
        </div>
        <span className="spacer" />
        <button onClick={() => setImporting((value) => !value)}>Import Dockerfile</button>
        <Link className="button primary" to="/projects/new">New image</Link>
      </div>

      {importing && (
        <Card title="Import an existing Dockerfile or Containerfile">
          {importError && <Banner>{importError}</Banner>}
          <Field label="Project name" hint="Lowercase letters, digits and dashes.">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="imported-image" />
          </Field>
          <Field
            label="Dockerfile or Containerfile"
            hint="Paste the file, or upload it. The first FROM line becomes the base image; the project opens in Advanced mode."
          >
            <textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={"FROM alpine:3.22\nRUN apk add --no-cache git"}
            />
          </Field>
          <div className="row">
            <input
              type="file"
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (file) setText(await file.text());
              }}
            />
            <span className="spacer" />
            <button className="primary" onClick={runImport} disabled={!name || !text.trim()}>
              Import
            </button>
          </div>
        </Card>
      )}

      {error && <Banner>{error}</Banner>}
      {loading && !data && <p className="dim">Loading…</p>}

      {data && (data.length === 0 ? (
        <Empty>
          No projects yet. <Link to="/projects/new">Create your first image</Link>.
        </Empty>
      ) : (
        <Card>
          <table className="table">
            <thead>
              <tr>
                <th>Project</th>
                <th>Repository</th>
                <th>Base</th>
                <th>Mode</th>
                <th>Builds</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {data.map((project) => (
                <tr key={project.id} onClick={() => navigate(`/projects/${project.id}`)} style={{ cursor: "pointer" }}>
                  <td data-label="Project">
                    <strong>{project.name}</strong>
                    {project.base_update_available && <span className="pill running" style={{ marginLeft: 8 }}>base update</span>}
                    {project.description && <div className="faint">{project.description}</div>}
                  </td>
                  <td data-label="Repository" className="mono">{project.repository}</td>
                  <td data-label="Base" className="mono dim">{project.base_image ?? "-"}</td>
                  <td data-label="Mode">{project.mode === "gui" ? "GUI managed" : "Advanced"}</td>
                  <td data-label="Builds">{project.build_count ?? 0}</td>
                  <td data-label="Updated" className="dim">{formatWhen(project.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="row end" style={{ marginTop: "0.8rem" }}>
            <button className="ghost" onClick={reload}>Refresh</button>
          </div>
        </Card>
      ))}
    </>
  );
}
