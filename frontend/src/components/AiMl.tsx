import { useEffect, useState } from "react";
import { AimlDetail, AimlTestRow, api, Build, formatBytes, SlurmParams } from "../api";
import { CompatTable } from "../pages/AiMlWizard";
import { CodeBlock } from "./Training";
import { CopyLink } from "./DownloadLinks";
import { Banner, Card, Field } from "./ui";

const STATUS_ICON: Record<string, string> = { passed: "✓", failed: "✕", not_run: "–" };

function TestRows({ rows }: { rows: AimlTestRow[] }) {
  return (
    <div className="checks">
      {rows.map((row) => (
        <details key={row.id} className={`check-row ${row.status}`}>
          <summary>
            <span className="check-icon" aria-hidden="true">{STATUS_ICON[row.status]}</span>
            <span className="check-label">{row.label} <span className="faint">· {row.where}</span></span>
            <span className="check-summary faint">{row.summary}</span>
          </summary>
          <div className="check-body">
            <p className="faint">{row.what}</p>
            {row.exit_code !== undefined && row.exit_code !== null && <p>Exit code: <span className="mono">{row.exit_code}</span></p>}
            {row.steps.length > 0 && (
              <table className="table">
                <tbody>
                  {row.steps.map((step, index) => (
                    <tr key={index}>
                      <td data-label="Step">{step.name}</td>
                      <td data-label="Result" className={step.status === "failed" ? "warn-text" : ""}>
                        {step.status}{step.seconds !== undefined && step.seconds !== null ? ` (${step.seconds}s)` : ""}
                      </td>
                      <td data-label="Detail" className="faint">{step.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </details>
      ))}
    </div>
  );
}

function download(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/x-shellscript" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function SlurmGenerator({ build, defaults }: { build: Build; defaults: SlurmParams }) {
  const [params, setParams] = useState<SlurmParams>(defaults);
  const [result, setResult] = useState<{ script: string; filename: string; warnings: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof SlurmParams>(key: K, value: SlurmParams[K]) => {
    setResult(null);
    setParams((current) => ({ ...current, [key]: value }));
  };

  async function generate() {
    setError(null);
    try {
      setResult(await api.aimlSlurm(build.id, params));
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  return (
    <>
      <p className="faint" style={{ marginTop: 0 }}>
        Slurm reserves the GPUs through GRES. On each node the script checks that the job's cgroup blocks every GPU it
        was not given, then passes exactly the allocated GPUs to Podman by their CDI UUID names. It stops with
        <span className="mono"> LAYERSMITH_PREREQ_MISSING</span> when GRES, device isolation, the CDI spec, SELinux
        permission or the loaded image is missing. Never <span className="mono">--privileged</span>, never
        <span className="mono"> nvidia.com/gpu=all</span>.
      </p>
      <div className="row" style={{ gap: "1rem", marginBottom: "0.5rem" }}>
        <label className="check"><input type="radio" checked={params.kind === "test"} onChange={() => set("kind", "test")} /> Slurm GPU test</label>
        <label className="check"><input type="radio" checked={params.kind === "job"} onChange={() => set("kind", "job")} /> Batch job</label>
      </div>
      <div className="grid three">
        <Field label="Job name"><input value={params.job_name} onChange={(e) => set("job_name", e.target.value)} /></Field>
        <Field label="Partition"><input value={params.partition} onChange={(e) => set("partition", e.target.value)} /></Field>
        <Field label="Account (optional)"><input value={params.account} onChange={(e) => set("account", e.target.value)} /></Field>
        <Field label="Nodes"><input type="number" min={1} value={params.nodes} onChange={(e) => set("nodes", Number(e.target.value))} /></Field>
        <Field label="GPUs per node (GRES)"><input type="number" min={1} value={params.gpus_per_node} onChange={(e) => set("gpus_per_node", Number(e.target.value))} /></Field>
        <Field label="GPU type (optional)" hint="gres.conf Type=, e.g. h200"><input value={params.gpu_type} onChange={(e) => set("gpu_type", e.target.value)} /></Field>
        <Field label="CPUs per task"><input type="number" min={1} value={params.cpus_per_task} onChange={(e) => set("cpus_per_task", Number(e.target.value))} /></Field>
        <Field label="Memory"><input value={params.mem} onChange={(e) => set("mem", e.target.value)} /></Field>
        <Field label="Time limit"><input value={params.time} onChange={(e) => set("time", e.target.value)} /></Field>
        <Field label="Output log"><input value={params.output} onChange={(e) => set("output", e.target.value)} /></Field>
        <Field label="Error log"><input value={params.error} onChange={(e) => set("error", e.target.value)} /></Field>
        <Field label="Shared memory"><input value={params.shm_size} onChange={(e) => set("shm_size", e.target.value)} /></Field>
        <Field label="Network">
          <select value={params.network} onChange={(e) => set("network", e.target.value as SlurmParams["network"])}>
            <option value="none">none (single node)</option>
            <option value="host">host (multi-node NCCL)</option>
          </select>
        </Field>
        <Field label="SELinux">
          <select value={params.selinux} onChange={(e) => set("selinux", e.target.value as SlurmParams["selinux"])}>
            <option value="container_use_devices">Enforcing, container_use_devices boolean (recommended)</option>
            <option value="label_disable">label=disable for this container only</option>
          </select>
        </Field>
        <Field label="Working directory (optional)"><input value={params.workdir} onChange={(e) => set("workdir", e.target.value)} /></Field>
      </div>
      {params.kind === "job" && (
        <Field label="Command" hint="Runs with /bin/bash -c inside the container.">
          <input className="mono" value={params.command} onChange={(e) => set("command", e.target.value)} />
        </Field>
      )}
      <h4>Bind mounts</h4>
      {params.mounts.map((mount, index) => (
        <div className="row" key={index} style={{ gap: "0.5rem", marginBottom: "0.35rem" }}>
          <input className="mono" value={mount.source} aria-label="Host path" onChange={(e) => set("mounts", params.mounts.map((m, i) => i === index ? { ...m, source: e.target.value } : m))} />
          <span>→</span>
          <input className="mono" value={mount.target} aria-label="Container path" onChange={(e) => set("mounts", params.mounts.map((m, i) => i === index ? { ...m, target: e.target.value } : m))} />
          <select value={mount.mode} onChange={(e) => set("mounts", params.mounts.map((m, i) => i === index ? { ...m, mode: e.target.value as "ro" | "rw" } : m))}>
            <option value="ro">read-only</option>
            <option value="rw">read-write</option>
          </select>
          <button className="ghost" onClick={() => set("mounts", params.mounts.filter((_, i) => i !== index))}>Remove</button>
        </div>
      ))}
      <button className="ghost" onClick={() => set("mounts", [...params.mounts, { source: "/scratch", target: "/scratch", mode: "rw" }])}>Add mount</button>
      {error && <Banner>{error}</Banner>}
      <div className="row" style={{ marginTop: "0.8rem" }}>
        <button className="primary" onClick={generate}>Generate Slurm batch script</button>
        {result && <button onClick={() => download(result.filename, result.script)}>Download {result.filename}</button>}
      </div>
      {result && (
        <>
          {result.warnings.map((warning) => <Banner key={warning} kind="warn">{warning}</Banner>)}
          <CodeBlock code={result.script} />
          <p className="faint">
            On the cluster: <span className="mono">sbatch {result.filename}</span>. The image must be loaded on every node
            first. Paste the job's output below to record the {params.kind === "test" ? "Slurm test" : "exit code"}.
          </p>
        </>
      )}
    </>
  );
}

export function AiMlPanel({ build, onChange, reload }: { build: Build; onChange: (detail: AimlDetail) => void; reload: () => void }) {
  const detail = build.aiml!;
  const [report, setReport] = useState("");
  const [reportError, setReportError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const exporting = Object.values(detail.exports ?? {}).some((entry) => entry.status === "exporting");

  useEffect(() => {
    if (!exporting) return;
    const timer = setInterval(reload, 3000);
    return () => clearInterval(timer);
  }, [exporting]); // eslint-disable-line react-hooks/exhaustive-deps

  if (detail.error) return <Card title="AI / ML / GPU"><Banner>{detail.error}</Banner></Card>;
  const meta = detail.metadata;
  const recipe = detail.recipe;
  const ready = build.status === "ready";
  const primary = detail.exports.primary;
  const docker = detail.exports["docker-archive"];

  async function record() {
    setReportError(null);
    try {
      onChange(await api.aimlReport(build.id, report));
      setReport("");
    } catch (exc) {
      setReportError((exc as Error).message);
    }
  }

  async function exportDocker() {
    setExportError(null);
    try {
      await api.aimlExport(build.id, "docker-archive");
      reload();
    } catch (exc) {
      setExportError((exc as Error).message);
    }
  }

  return (
    <>
      <div className="grid two">
        <Card title={`${recipe.template_name} ${recipe.version}`}>
          <dl className="kv">
            <dt>Mode</dt><dd>{recipe.mode === "import" ? "Upstream image, unchanged (labels only)" : "Custom image"}</dd>
            <dt>Base image</dt><dd className="mono">{meta.base}</dd>
            <dt>Base digest</dt><dd className="mono truncate-long" title={meta.base_digest ?? ""}>{meta.base_digest ?? "—"}
              {meta.base_verified ? <span className="faint"> · verified by LayerSmith</span> : <span className="warn-text"> · unverified base</span>}</dd>
            <dt>CUDA</dt><dd>{meta.cuda ?? "—"}</dd>
            {meta.torch && <><dt>PyTorch</dt><dd>{meta.torch} <span className="faint">(CUDA {meta.torch_cuda})</span></dd></>}
            {meta.tensorflow && <><dt>TensorFlow</dt><dd>{meta.tensorflow}</dd></>}
            <dt>Python</dt><dd>{meta.python ?? "—"}</dd>
            <dt>OS</dt><dd>{meta.os ?? "—"}</dd>
            {meta.cudnn && <><dt>cuDNN</dt><dd>{meta.cudnn}</dd></>}
            {meta.nccl && <><dt>NCCL</dt><dd>{meta.nccl}</dd></>}
            <dt>GPU architectures</dt><dd className="mono">{meta.compiled_archs?.length ? meta.compiled_archs.join(" ") : "read by the basic test"}</dd>
            <dt>Image size</dt><dd>{formatBytes(meta.image_size)}</dd>
            <dt>Installed software</dt><dd>{meta.package_count ? `${meta.package_count} Python packages` : "—"}
              {meta.requested.length > 0 && <span className="faint"> · added: {meta.requested.join(", ")}</span>}</dd>
            <dt>Build status</dt><dd>{build.status}</dd>
          </dl>
          {ready && (
            <p className="faint">
              <a href={api.aimlFileUrl(build.id, "packages.json")}>packages.json</a> ·{" "}
              <a href={api.aimlFileUrl(build.id, "requirements.lock")}>requirements.lock</a> ·{" "}
              <a href={api.aimlFileUrl(build.id, "sbom.cdx.json")}>SBOM (CycloneDX)</a> ·{" "}
              <a href={api.aimlFileUrl(build.id, "README.md")}>README.md</a>
            </p>
          )}
        </Card>

        <Card title="GPU compatibility">
          <p className="faint" style={{ marginTop: 0 }}>
            Target: {String(detail.target.os)}, driver {String(detail.target.driver)}, Podman + NVIDIA CDI. A detected GPU
            is not proof: these rows say what each verdict rests on.
          </p>
          <CompatTable rows={detail.compatibility} />
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}><Card title="Tests">
        <TestRows rows={detail.tests} />
        <h4>GPU test on a worker</h4>
        <p className="faint">
          The build server needs no GPU. On a GPU worker, with <span className="mono">layersmith-aiml-check</span> from the
          bundle (or <a href={api.aimlFileUrl(build.id, "layersmith-aiml-check")}>download it</a>):
        </p>
        <CodeBlock code={detail.offline.gpu_test} />
        <Field label="Record a GPU or Slurm test" hint="Paste the whole output (for Slurm: the job's .out file). Only results for this exact image are accepted.">
          <textarea rows={4} value={report} onChange={(event) => setReport(event.target.value)} />
        </Field>
        {reportError && <Banner>{reportError}</Banner>}
        <button onClick={record} disabled={!report.trim() || !ready}>Record result</button>
      </Card></div>

      <div style={{ marginTop: "1rem" }}><Card title="Export for the offline GPU cluster">
        {!ready ? <p className="dim">Available when the build is ready.</p> : (
          <>
            <table className="table">
              <tbody>
                <tr>
                  <td data-label="Archive" className="mono">{primary.archive}</td>
                  <td data-label="Format">{primary.format}</td>
                  <td data-label="Size">{formatBytes(primary.size)}</td>
                  <td data-label="SHA-256" className="mono split-sha" title={primary.sha256 ?? ""}>{primary.sha256}</td>
                  <td data-label="">
                    {primary.available && <><a className="button" href={api.downloadUrl(build.id, "export")}>Download</a>{" "}
                      <a href={api.downloadUrl(build.id, "export.sha256")}>.sha256</a>{" "}
                      <CopyLink url={api.downloadUrl(build.id, "export")} name={primary.archive} /></>}
                  </td>
                </tr>
                {/* A Docker backend already writes a Docker archive; only Podman's OCI export needs the second format. */}
                {primary.format?.startsWith("OCI") && <tr>
                  <td data-label="Archive" className="mono">{docker?.archive ?? primary.archive.replace(/\.tar$/, ".docker.tar")}</td>
                  <td data-label="Format">Docker archive (podman load reads both)</td>
                  <td data-label="Size">{docker?.size ? formatBytes(docker.size) : "—"}</td>
                  <td data-label="SHA-256" className="mono split-sha" title={docker?.sha256 ?? ""}>{docker?.sha256 ?? ""}</td>
                  <td data-label="">
                    {docker?.available ? (
                      <><a className="button" href={api.aimlFileUrl(build.id, "docker-archive")}>Download</a>{" "}
                        <a href={api.aimlFileUrl(build.id, "docker-archive.sha256")}>.sha256</a>{" "}
                        <CopyLink url={api.aimlFileUrl(build.id, "docker-archive")} name={docker.archive} /></>
                    ) : docker?.status === "exporting" ? <span className="dim">Exporting…</span> : (
                      <button onClick={exportDocker}>Export</button>
                    )}
                  </td>
                </tr>}
              </tbody>
            </table>
            {docker?.status === "failed" && <Banner>{docker.error}</Banner>}
            {exportError && <Banner>{exportError}</Banner>}
            <p className="faint">
              The air-gap bundle (Image card) holds the archive and its <span className="mono">.sha256</span>, the
              Containerfile, the manifest, <span className="mono">aiml/packages.json</span>, a CycloneDX SBOM and the test
              script. Nothing is downloaded when the container starts.
            </p>
            <h4>On the isolated AlmaLinux GPU worker</h4>
            <CodeBlock code={`${detail.offline.verify}\n\n${detail.offline.load}\n\n${detail.offline.list}`} />
            <CodeBlock code={detail.offline.run.podman} />
            {detail.offline.run.note && <p className="faint">{detail.offline.run.note}</p>}
          </>
        )}
      </Card></div>

      <div style={{ marginTop: "1rem" }}><Card title="Slurm">
        <SlurmGenerator build={build} defaults={detail.slurm_defaults} />
      </Card></div>
    </>
  );
}
