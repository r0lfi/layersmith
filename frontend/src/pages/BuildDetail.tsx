import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, buildLogSocket, formatBytes, formatDuration, formatWhen } from "../api";
import { Banner, Card, StatusPill, isActive, useLoad } from "../components/ui";
import { SecurityPanel } from "../components/Security";

function LogLine({ line }: { line: string }) {
  const className = /^STEP |^--> /.test(line)
    ? "step"
    : /^ERROR|^error|failed/i.test(line)
    ? "err"
    : /^WARNING/i.test(line)
    ? "warnline"
    : line.startsWith("$ ")
    ? "cmd"
    : undefined;
  return <div className={className}>{line || " "}</div>;
}

export default function BuildDetail() {
  const { id = "" } = useParams();
  const { data: build, error, reload } = useLoad(() => api.build(id), [id]);
  const { data: settings } = useLoad(() => api.settings(), []);
  const [lines, setLines] = useState<string[]>([]);
  const [live, setLive] = useState(false);
  const [follow, setFollow] = useState(true);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  // One socket per build: it replays the log from the start, then streams.
  useEffect(() => {
    if (!id) return;
    setLines([]);
    const socket = buildLogSocket(id);
    socket.onopen = () => setLive(true);
    socket.onclose = () => setLive(false);
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "log") setLines((current) => [...current, message.line]);
      if (message.type === "done" || message.type === "status") reload();
    };
    return () => socket.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (follow && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [lines, follow]);

  if (error) return <Banner>{error}</Banner>;
  if (!build) return <p className="dim">Loading…</p>;

  const running = isActive(build.status);
  const shown = lines.length ? lines : (build.log ?? "").split("\n");

  async function makeBundle() {
    setBusy(true);
    setActionError(null);
    try {
      await api.airgap(build!.id);
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
          <h1>Build #{build.number}</h1>
          <p className="dim">
            <Link to={`/projects/${build.project_id}`}>{build.project_name ?? "project"}</Link>{" "}
            <span className="mono">{build.version}</span>
          </p>
        </div>
        <span className="spacer" />
        <StatusPill status={build.status} />
      </div>

      {build.error && <Banner>{build.error}</Banner>}
      {actionError && <Banner>{actionError}</Banner>}

      <div className="grid two">
        <Card title="Image">
          <table className="table">
            <tbody>
              <tr><td data-label="Image">Image</td><td className="mono">{build.image_ref ?? "-"}</td></tr>
              <tr><td data-label="Base">Base</td><td className="mono">{build.base_image ?? "-"}</td></tr>
              <tr>
                <td data-label="Base digest">Base digest</td>
                <td className="mono truncate" title={build.base_digest ?? ""}>{build.base_digest ?? "-"}</td>
              </tr>
              <tr>
                <td data-label="Image digest">Image digest</td>
                <td className="mono truncate" title={build.image_digest ?? ""}>{build.image_digest ?? "-"}</td>
              </tr>
              <tr><td data-label="Architecture">Architecture</td><td className="mono">{build.architecture}</td></tr>
              <tr><td data-label="Image size">Image size</td><td>{formatBytes(build.image_size)}</td></tr>
              <tr><td data-label="Started">Started</td><td className="dim">{formatWhen(build.started_at)}</td></tr>
              <tr><td data-label="Duration">Duration</td><td>{formatDuration(build.duration_seconds)}</td></tr>
            </tbody>
          </table>

          {build.status === "ready" && (
            <div className="grid two" style={{ marginTop: "1rem" }}>
              <div>
                <h3>Image archive</h3>
                <p className="faint" style={{ marginTop: "0.2rem" }}>
                  The image on its own, as <span className="mono">podman save</span> /{" "}
                  <span className="mono">docker save</span> writes it. Load it with{" "}
                  <span className="mono">load -i</span> on a machine that already knows what it is getting.
                </p>
                {build.has_export && (
                  <a className="button" href={api.downloadUrl(build.id, "export")}>
                    Download TAR ({formatBytes(build.export_size)})
                  </a>
                )}
              </div>
              <div>
                <h3>Air-gap bundle</h3>
                <p className="faint" style={{ marginTop: "0.2rem" }}>
                  The same image plus what an offline host needs to verify and understand it: a manifest
                  (base image and digest, packages, checksums), the Containerfile it was built from,
                  <span className="mono"> SHA256SUMS</span> and <span className="mono">INSTALL.txt</span>.
                  Roughly the same size as the TAR.
                </p>
                {build.has_airgap ? (
                  <a className="button primary" href={api.downloadUrl(build.id, "airgap")}>
                    Download air-gap bundle ({formatBytes(build.airgap_size)})
                  </a>
                ) : (
                  <button className="primary" onClick={makeBundle} disabled={busy}>
                    {busy ? "Bundling…" : "Create air-gap bundle"}
                  </button>
                )}
              </div>
            </div>
          )}
          {build.export_sha256 && (
            <p className="faint mono" style={{ wordBreak: "break-all", marginTop: "0.6rem" }}>
              sha256:{build.export_sha256}
            </p>
          )}
          {build.packages.length > 0 && (
            <>
              <h3 style={{ marginTop: "1rem" }}>Packages ({build.packages.length})</h3>
              <p className="mono faint" style={{ wordBreak: "break-word" }}>{build.packages.join(" ")}</p>
            </>
          )}
        </Card>

        <Card title="Containerfile">
          <pre className="code">{build.containerfile || "-"}</pre>
        </Card>
      </div>

      {build.status === "ready" && (
        <SecurityPanel
          buildId={build.id}
          scannerAvailable={!!settings?.scanner?.available}
          scannerDetail={settings?.scanner?.detail ?? ""}
        />
      )}

      <Card
        title="Build log"
        actions={
          <div className="row">
            {running && (
              <span className="faint" style={{ fontSize: "0.8rem" }}>
                {live ? "streaming" : "reconnecting…"}
              </span>
            )}
            <label className="check">
              <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
              <span>Follow</span>
            </label>
          </div>
        }
      >
        <div className="log" ref={logRef}>
          {shown.map((line, index) => (
            <LogLine key={index} line={line} />
          ))}
        </div>
      </Card>
    </>
  );
}
