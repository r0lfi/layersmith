import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api, formatBytes, formatDuration, formatWhen } from "../api";
import { Banner, Card, Empty, StatusPill, isActive, useLoad, usePoll } from "../components/ui";

export default function Builds() {
  const navigate = useNavigate();
  const { data, error, loading, reload } = useLoad(() => api.builds(100));
  const refresh = useCallback(() => reload(), [reload]);
  usePoll(refresh, Boolean(data?.some((build) => isActive(build.status))));

  if (error) return <Banner>{error}</Banner>;
  if (loading && !data) return <p className="dim">Loading…</p>;

  return (
    <>
      <div className="row">
        <h1>Builds</h1>
        <span className="spacer" />
        <button className="ghost" onClick={refresh}>Refresh</button>
      </div>

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
              </tr>
            </thead>
            <tbody>
              {data?.map((build) => (
                <tr key={build.id} onClick={() => navigate(`/builds/${build.id}`)} style={{ cursor: "pointer" }}>
                  <td data-label="Build" className="mono">#{build.number}</td>
                  <td data-label="Project">{build.project_name ?? "-"}</td>
                  <td data-label="Version" className="mono">{build.version}</td>
                  <td data-label="Started" className="dim">{formatWhen(build.started_at ?? build.created_at)}</td>
                  <td data-label="Duration">{formatDuration(build.duration_seconds)}</td>
                  <td data-label="Size">{formatBytes(build.image_size)}</td>
                  <td data-label="Status"><StatusPill status={build.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </>
  );
}
