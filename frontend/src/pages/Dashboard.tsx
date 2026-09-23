import { useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, formatBytes, formatWhen } from "../api";
import { Banner, Card, Empty, Stat, StatusPill, isActive, useLoad, usePoll } from "../components/ui";

const QUICK_BUILD = ["Developer", "Linux Admin", "Network Tools", "Ansible", "Kubernetes", "Minimal"];

export default function Dashboard() {
  const navigate = useNavigate();
  const { data, error, loading, reload } = useLoad(() => api.stats());
  const refresh = useCallback(() => reload(), [reload]);
  usePoll(refresh, Boolean(data?.recent.some((build) => isActive(build.status))));

  if (loading && !data) return <p className="dim">Loading…</p>;
  if (error) return <Banner>{error}</Banner>;
  if (!data) return null;

  return (
    <>
      <h1>LayerSmith</h1>
      <p className="dim">Build container images with purpose.</p>

      <div className="grid stats" style={{ marginTop: "1.25rem" }}>
        <Stat value={data.images} label="Images" />
        <Stat value={data.projects} label="Projects" />
        <Stat value={data.builds} label="Builds" />
        <Stat value={formatBytes(data.storage_bytes)} label="Storage" />
      </div>

      <div style={{ height: "1rem" }} />

      <Card title="Quick build">
        <p className="faint" style={{ marginTop: 0 }}>
          Start a new project from a template. You choose the base image and tools next.
        </p>
        <div className="grid cards">
          {QUICK_BUILD.map((template) => (
            <button
              key={template}
              className="tile"
              onClick={() => navigate(`/projects/new?template=${encodeURIComponent(template)}`)}
            >
              <div className="tile-title">{template}</div>
              <div className="tile-sub">Start from this template</div>
            </button>
          ))}
        </div>
      </Card>

      <Card
        title="Recent builds"
        actions={<Link className="button" to="/builds">All builds</Link>}
      >
        {data.recent.length === 0 ? (
          <Empty>
            No builds yet. <Link to="/projects/new">Create your first image</Link>.
          </Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Build</th>
                <th>Project</th>
                <th>Version</th>
                <th>Started</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.recent.map((build) => (
                <tr key={build.id} onClick={() => navigate(`/builds/${build.id}`)} style={{ cursor: "pointer" }}>
                  <td data-label="Build" className="mono">#{build.number}</td>
                  <td data-label="Project">{build.project_name ?? "-"}</td>
                  <td data-label="Version" className="mono">{build.version}</td>
                  <td data-label="Started" className="dim">{formatWhen(build.started_at ?? build.created_at)}</td>
                  <td data-label="Status"><StatusPill status={build.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
