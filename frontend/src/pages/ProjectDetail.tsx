import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, formatBytes, formatWhen } from "../api";
import { Banner, Card, Empty, Field, StatusPill, useLoad } from "../components/ui";

export default function ProjectDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { data: project, error, loading, reload } = useLoad(() => api.project(id), [id]);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [version, setVersion] = useState("");
  const [editing, setEditing] = useState(false);
  const [containerfile, setContainerfile] = useState("");

  if (error) return <Banner>{error}</Banner>;
  if (loading && !project) return <p className="dim">Loading…</p>;
  if (!project) return null;

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    try {
      await action();
      reload();
    } catch (exc) {
      setActionError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="row">
        <div>
          <h1>{project.name}</h1>
          <p className="dim mono">{project.repository}</p>
        </div>
        <span className="spacer" />
        <button
          onClick={() =>
            act(async () => {
              const name = prompt("Name for the clone", `${project.name}-custom`);
              if (name) {
                const clone = await api.cloneProject(project.id, name);
                navigate(`/projects/${clone.id}`);
              }
            })
          }
          disabled={busy}
        >
          Clone
        </button>
        <button
          className="primary"
          disabled={busy}
          onClick={() =>
            act(async () => {
              const build = await api.startBuild(project.id, version || undefined);
              navigate(`/builds/${build.id}`);
            })
          }
        >
          Build {version || project.next_version}
        </button>
      </div>

      {actionError && <Banner>{actionError}</Banner>}
      {project.base_update_available && (
        <Banner kind="info">
          A newer base image is available. Building a new version picks it up; existing images are left alone.
        </Banner>
      )}
      {project.warnings?.map((warning) => (
        <Banner kind="warn" key={warning}>{warning}</Banner>
      ))}

      <div className="grid two">
        <Card title="Definition">
          <table className="table">
            <tbody>
              <tr><td data-label="Template">Template</td><td>{project.template}</td></tr>
              <tr><td data-label="Mode">Mode</td><td>{project.mode === "gui" ? "GUI managed" : "Advanced"}</td></tr>
              <tr><td data-label="Base">Base</td><td className="mono">{project.base_image ?? "-"}</td></tr>
              <tr>
                <td data-label="Base digest">Base digest</td>
                <td className="mono truncate">{project.base_digest ?? "not pulled yet"}</td>
              </tr>
              <tr><td data-label="Architecture">Architecture</td><td className="mono">{project.spec?.architecture ?? "amd64"}</td></tr>
              <tr><td data-label="Next version">Next version</td><td className="mono">{project.next_version}</td></tr>
            </tbody>
          </table>
          <Field label="Build a specific version" hint="Leave empty to use the next version.">
            <input value={version} onChange={(event) => setVersion(event.target.value)} placeholder={project.next_version} />
          </Field>
          {project.packages && project.packages.length > 0 && (
            <>
              <h3>Resolved packages</h3>
              <p className="mono faint" style={{ wordBreak: "break-word" }}>{project.packages.join(" ")}</p>
            </>
          )}
        </Card>

        <Card
          title="Containerfile"
          actions={
            project.mode === "advanced" ? (
              editing ? (
                <div className="row">
                  <button className="ghost" onClick={() => setEditing(false)}>Cancel</button>
                  <button
                    className="primary"
                    onClick={() =>
                      act(async () => {
                        await api.updateProject(project.id, {
                          name: project.name,
                          description: project.description,
                          repository: project.repository,
                          template: project.template,
                          mode: "advanced",
                          containerfile,
                        });
                        setEditing(false);
                      })
                    }
                  >
                    Save
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => {
                    setContainerfile(project.containerfile);
                    setEditing(true);
                  }}
                >
                  Edit
                </button>
              )
            ) : (
              <span className="faint" style={{ fontSize: "0.8rem" }}>Generated from the definition</span>
            )
          }
        >
          {editing ? (
            <textarea value={containerfile} onChange={(event) => setContainerfile(event.target.value)} />
          ) : (
            <pre className="code">{project.containerfile || "No Containerfile yet."}</pre>
          )}
        </Card>
      </div>

      <Card title="Builds">
        {!project.builds || project.builds.length === 0 ? (
          <Empty>No builds yet.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Build</th>
                <th>Version</th>
                <th>Started</th>
                <th>Size</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {project.builds.map((build) => (
                <tr key={build.id} onClick={() => navigate(`/builds/${build.id}`)} style={{ cursor: "pointer" }}>
                  <td data-label="Build" className="mono">#{build.number}</td>
                  <td data-label="Version" className="mono">{build.version}</td>
                  <td data-label="Started" className="dim">{formatWhen(build.started_at ?? build.created_at)}</td>
                  <td data-label="Size">{formatBytes(build.image_size)}</td>
                  <td data-label="Status"><StatusPill status={build.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <div className="row end" style={{ marginTop: "1rem" }}>
        <button
          className="danger"
          disabled={busy}
          onClick={() =>
            act(async () => {
              if (confirm(`Delete project ${project.name}? Built images and archives are kept.`)) {
                await api.deleteProject(project.id);
                navigate("/projects");
              }
            })
          }
        >
          Delete project
        </button>
      </div>
    </>
  );
}
