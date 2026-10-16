import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, Build, formatBytes, formatDuration, formatWhen } from "../api";
import { Banner, Card, Empty, StatusPill, isActive, useLoad, usePoll } from "../components/ui";

export default function Builds() {
  const navigate = useNavigate();
  const { data, error, loading, reload } = useLoad(() => api.builds(100));
  const refresh = useCallback(() => reload(), [reload]);
  usePoll(refresh, Boolean(data?.some((build) => isActive(build.status))));
  const [deleting, setDeleting] = useState<string | null>(null);
  const [message, setMessage] = useState<{ error: boolean; text: string } | null>(null);

  async function remove(build: Build) {
    const archive = build.export_size ? ` its ${formatBytes(build.export_size)} archive,` : "";
    const confirmed = window.confirm(
      `Delete build #${build.number} (${build.project_name ?? "project"} ${build.version})?\n\n` +
      `This removes the build record,${archive} its air-gap bundle, log and scan results, and the image ` +
      `${build.image_ref ?? ""} from the local image store. Files and images another build still uses are kept.\n\n` +
      "This cannot be undone.");
    if (!confirmed) return;
    setDeleting(build.id);
    setMessage(null);
    try {
      const result = await api.deleteBuild(build.id);
      const freed = result.freed_bytes ? `, ${formatBytes(result.freed_bytes)} freed` : "";
      setMessage({ error: false, text: `Build #${result.number} deleted${freed}.${result.notes.length ? " " + result.notes.join(". ") + "." : ""}` });
      reload();
    } catch (exc) {
      setMessage({ error: true, text: (exc as Error).message });
    } finally {
      setDeleting(null);
    }
  }

  if (error) return <Banner>{error}</Banner>;
  if (loading && !data) return <p className="dim">Loading…</p>;

  return (
    <>
      <div className="row">
        <h1>Builds</h1>
        <span className="spacer" />
        <button className="ghost" onClick={refresh}>Refresh</button>
      </div>

      {message && (message.error
        ? <Banner>{message.text}</Banner>
        : <p className="faint" role="status">{message.text}</p>)}

      {data && data.length === 0 ? (
        <Empty>No builds yet.</Empty>
      ) : (
        <Card>
          <table className="table">
            <thead>
              <tr>
                <th>Build</th>
                <th>Project</th>
                <th>Version</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Size</th>
                <th>Status</th>
                <th aria-label="Actions"></th>
              </tr>
            </thead>
            <tbody>
              {data?.map((build) => (
                <tr key={build.id} onClick={() => navigate(`/builds/${build.id}`)} style={{ cursor: "pointer" }}>
                  <td data-label="Build" className="mono">#{build.number}</td>
                  <td data-label="Project">
                    {build.project_name ?? "-"}
                    {build.project_deleted && <span className="faint"> (deleted project)</span>}
                  </td>
                  <td data-label="Version" className="mono">{build.version}</td>
                  <td data-label="Started" className="dim">{formatWhen(build.started_at ?? build.created_at)}</td>
                  <td data-label="Duration">{formatDuration(build.duration_seconds)}</td>
                  <td data-label="Size">{formatBytes(build.image_size)}</td>
                  <td data-label="Status"><StatusPill status={build.status} /></td>
                  <td>
                    <button
                      className="danger"
                      disabled={isActive(build.status) || deleting !== null}
                      title={isActive(build.status) ? "A running build cannot be deleted" : `Delete build #${build.number}`}
                      onClick={(event) => {
                        event.stopPropagation();
                        remove(build);
                      }}
                    >
                      {deleting === build.id ? "Deleting…" : "Delete"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </>
  );
}
