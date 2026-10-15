import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Build, buildLogSocket, formatBytes, formatDuration, formatWhen } from "../api";
import { Banner, Card, StatusPill, isActive, useLoad } from "../components/ui";
import { SecurityPanel } from "../components/Security";
import { TrainingPanel } from "../components/Training";

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

function GpuBenchmarkPanel({ build }: { build: Build }) {
  const detail = build.gpu_benchmark!;
  const started = detail.getting_started;
  const exported = detail.export;
  const [name, tag] = splitRef(build.image_ref ?? started.image);
  return (
    <div style={{ marginTop: "1rem" }}>
      <Card title="NVIDIA GPU & LLM Benchmark">
        <p className="faint" style={{ marginTop: 0 }}>{started.summary}</p>
        <div className="grid two">
          <div>
            <dl className="kv">
              <dt>Image name</dt><dd className="mono">{name}</dd>
              <dt>Image tag</dt><dd className="mono">{tag}</dd>
              <dt>Image ID</dt><dd className="mono truncate" title={build.image_id ?? ""}>{build.image_id ?? "—"}</dd>
              <dt>Digest</dt><dd className="mono truncate" title={build.image_digest ?? ""}>{build.image_digest ?? "— (local image, not pushed)"}</dd>
              <dt>File</dt><dd className="mono">{exported.archive}</dd>
              <dt>File size</dt><dd>{formatBytes(exported.size)}</dd>
              <dt>SHA-256</dt><dd className="mono" style={{ wordBreak: "break-all" }}>{exported.sha256 ?? "—"}</dd>
              <dt>Format</dt><dd>{exported.format}</dd>
              <dt>Export status</dt><dd>{exported.status}</dd>
              <dt>Versions</dt><dd className="faint">
                CUDA {detail.recipe.cuda_version} · Python {detail.recipe.python_full_version} · PyTorch {detail.recipe.torch}
                {detail.recipe.vllm ? ` · vLLM ${detail.recipe.vllm}` : " · no vLLM"} · driver ≥ {detail.recipe.driver_minimum}
              </dd>
            </dl>
            <div className="row" style={{ marginTop: "1rem", gap: "0.5rem", flexWrap: "wrap" }}>
              {exported.available ? (
                <>
                  <a className="button primary" href={api.downloadUrl(build.id, "export")}>
                    Download Podman Image (.tar) · {formatBytes(exported.size)}
                  </a>
                  <a className="button" href={api.downloadUrl(build.id, "export.sha256")}>SHA-256 file</a>
                </>
              ) : (
                <span className="faint">
                  {build.status === "ready" ? "The exported archive is no longer on disk." : "Available when the build is ready."}
                </span>
              )}
            </div>
          </div>
          <div>
            <h3 style={{ marginTop: 0 }}>1. Check the GPU server</h3>
            <pre className="code">{started.check_host}</pre>
            <h3>2. Verify and load (works offline)</h3>
            <pre className="code">{`${started.verify}\n${started.load}`}</pre>
            <h3>3. Start the benchmark dashboard</h3>
            <pre className="code">{started.podman}</pre>
          </div>
        </div>
        <h3>Scripted runs (CLI, Slurm)</h3>
        <pre className="code">{started.batch_example}</pre>
        <pre className="code">{started.commands.join("\n")}</pre>
        <ul className="faint">{started.notes.map((note) => <li key={note}>{note}</li>)}</ul>
      </Card>
    </div>
  );
}

function splitRef(reference: string): [string, string] {
  const index = reference.lastIndexOf(":");
  return index > 0 ? [reference.slice(0, index), reference.slice(index + 1)] : [reference, "latest"];
}

export default function BuildDetail() {
  const { id = "" } = useParams();
  const { data: build, error, reload, setData } = useLoad(() => api.build(id), [id]);
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

      {build.training && (
        <TrainingPanel
          buildId={build.id}
          detail={build.training}
          ready={build.status === "ready"}
          onChange={(training) => setData({ ...build, training })}
        />
      )}

      {build.gpu_benchmark && <GpuBenchmarkPanel build={build} />}

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
