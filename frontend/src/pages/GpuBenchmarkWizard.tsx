import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, GpuBenchmarkCatalog, GpuBenchmarkMirrorKey, GpuBenchmarkRequest, GpuBenchmarkResolve } from "../api";
import { Banner, Card, Field } from "../components/ui";

const TEMPLATE = "NVIDIA GPU & LLM Benchmark";

/** What each selectable component adds, shown under its checkbox. */
const COMPONENT_DETAIL: Record<string, string> = {
  llm: "Token speed, TTFT/TPOT, P50/P95/P99 latency, single-request vs. aggregate throughput on local models.",
  compute: "Measured TFLOPS per precision and per GPU, CUDA-event timed.",
  memory: "VRAM/HBM, host↔device (PCIe) and GPU↔GPU bandwidth, plus NVIDIA nvbandwidth.",
  nccl: "all_reduce, all_gather, reduce_scatter, broadcast, sendrecv, alltoall; multi-GPU on one server and "
    + "multi-node over RDMA/RoCE with MPI, torchrun or Slurm.",
};

const MIRRORS: [GpuBenchmarkMirrorKey, string][] = [
  ["apt_main", "Ubuntu APT mirror"],
  ["apt_security", "Ubuntu security mirror"],
  ["pypi_index", "Python package index"],
  ["torch_index", "PyTorch wheel index (CUDA 12.8 only)"],
  ["python_mirror", "CPython distribution mirror"],
  ["nccl_tests_repository", "nccl-tests Git mirror"],
  ["nvbandwidth_repository", "nvbandwidth Git mirror"],
];

export default function GpuBenchmarkWizard({ onLeave }: { onLeave: () => void }) {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<GpuBenchmarkCatalog | null>(null);
  const [request, setRequest] = useState<GpuBenchmarkRequest>({
    cuda_version: "13.0.2", python_version: "3.12", tools: ["llm", "compute", "memory", "nccl"],
  });
  const [name, setName] = useState("gpu-benchmark");
  const [repository, setRepository] = useState("localhost/gpu-benchmark");
  const [tag, setTag] = useState("1.0.0");
  const [description, setDescription] = useState(
    "Benchmark NVIDIA GPUs and AI models: token speed, inference latency, GPU compute, memory bandwidth and multi-GPU performance.",
  );
  const [resolved, setResolved] = useState<GpuBenchmarkResolve | null>(null);
  const [containerfile, setContainerfile] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.gpuBenchmarkCatalog().then(setCatalog, (exc: Error) => setError(exc.message));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setContainerfile("");
    api.gpuBenchmarkResolve(request).then((value) => {
      if (!cancelled) setResolved(value);
    }, (exc: Error) => {
      if (!cancelled) setError(exc.message);
    });
    return () => { cancelled = true; };
  }, [request]);

  const spec = useMemo(() => ({ gpu_benchmark: request }), [request]);
  const imageRef = `${repository || name}:${tag}`;
  const archive = `${(repository || name).replace(/^localhost\//, "").replace(/\//g, "-")}-${tag}.tar`;
  const validImage = /^[a-z0-9][a-z0-9-]*$/.test(name)
    && /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,3}$/.test(repository)
    && /^\d+\.\d+\.\d+$/.test(tag);

  function toggle(tool: string, checked: boolean) {
    setRequest((current) => ({
      ...current,
      tools: checked ? [...current.tools, tool] : current.tools.filter((value) => value !== tool),
    }));
  }

  async function preview() {
    setError(null);
    try {
      const value = await api.preview({ name: name || "gpu-benchmark", description, template: TEMPLATE, spec });
      setContainerfile(value.containerfile);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  async function build() {
    if (!resolved?.ok || !validImage) return;
    setBusy(true);
    setError(null);
    try {
      const project = await api.createProject({ name, description, repository, template: TEMPLATE, mode: "gui", spec });
      const result = await api.startBuild(project.id, tag);
      navigate(`/builds/${result.id}`);
    } catch (exc) {
      setError((exc as Error).message);
      setBusy(false);
    }
  }

  if (!catalog) return error ? <Banner>{error}</Banner> : <p className="dim">Loading NVIDIA benchmark options…</p>;

  const recipe = resolved?.recipe;
  const storage = resolved?.storage ?? catalog.storage;
  const variant = catalog.cuda_variants[request.cuda_version];
  const fill = storage.free_gb == null ? 0 : Math.min(100, (storage.required_free_gb / Math.max(storage.free_gb, 0.1)) * 100);

  return (
    <div style={{ marginTop: "1rem" }}>
      <div className="row" style={{ marginBottom: "1rem" }}>
        <div>
          <h2 style={{ margin: 0 }}>{catalog.category.name}</h2>
          <p className="dim">{catalog.category.intro}</p>
        </div>
        <span className="spacer" />
        <button onClick={onLeave}>Back to general images</button>
      </div>
      {error && <Banner>{error}</Banner>}
      {resolved && !resolved.ok && <Banner kind="warn">{resolved.error}</Banner>}

      <div className="grid two">
        <Card title="Image">
          <div className="grid two">
            <Field label="Image name (project)"><input value={name} onChange={(event) => setName(event.target.value)} /></Field>
            <Field label="Image tag (version)"><input value={tag} onChange={(event) => setTag(event.target.value)} /></Field>
          </div>
          <Field label="Repository" hint="localhost/… keeps the name Podman shows after podman load.">
            <input value={repository} onChange={(event) => setRepository(event.target.value)} />
          </Field>
          <Field label="Description"><input value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
          <dl className="kv" style={{ marginTop: "0.6rem" }}>
            <dt>Image</dt><dd className="mono">{imageRef}</dd>
            <dt>Archive</dt><dd className="mono">{archive}</dd>
            <dt>Architecture</dt><dd>Linux x86_64 (amd64)</dd>
            <dt>Runtime</dt><dd>Podman (NVIDIA CDI: <span className="mono">--device nvidia.com/gpu=all</span>)</dd>
          </dl>
          <p className="faint">
            Versions are immutable in LayerSmith: a new build gets a new tag (1.0.1, 1.1.0 …) and never replaces an
            existing archive, so a benchmark result always names exactly one image.
          </p>
        </Card>

        <Card title="CUDA, Python, PyTorch and vLLM">
          <Field label="CUDA">
            <select value={request.cuda_version} onChange={(event) => setRequest({ ...request, cuda_version: event.target.value })}>
              {Object.entries(catalog.cuda_variants).map(([version, entry]) => (
                <option key={version} value={version}>{entry.label}</option>
              ))}
            </select>
          </Field>
          <Field label="Python">
            <select value={request.python_version} onChange={(event) => setRequest({ ...request, python_version: event.target.value })}>
              {Object.entries(catalog.python_versions).map(([version, exact]) => (
                <option key={version} value={version}>{version} (pinned {exact})</option>
              ))}
            </select>
          </Field>
          <dl className="kv">
            <dt>Base</dt><dd className="mono">{variant?.base}</dd>
            <dt>PyTorch</dt><dd>{recipe?.torch ?? variant?.torch} <span className="faint">· matched to CUDA automatically</span></dd>
            <dt>vLLM</dt><dd>{recipe ? (recipe.vllm ?? "not installed") : variant?.vllm ?? "—"}
              {variant?.vllm && <span className="faint"> · pins this PyTorch exactly</span>}</dd>
            <dt>Host driver</dt><dd>≥ {variant?.driver_minimum}</dd>
            <dt>GPU kernels</dt><dd className="mono">{recipe?.gpu_architectures.map((a) => `sm_${a}`).join(" ")}</dd>
            <dt>Targets</dt><dd>NVIDIA H200 (Hopper, sm_90) · RTX PRO 6000 Blackwell (sm_120) · other sm_75+ GPUs</dd>
          </dl>
        </Card>
      </div>

      <div className="grid two" style={{ marginTop: "1rem" }}>
        <Card title="Benchmark components">
          {Object.entries(catalog.tool_groups).map(([id, label]) => (
            <label className="check" key={id} style={{ display: "flex", alignItems: "flex-start", margin: "0.65rem 0", gap: "0.5rem" }}>
              <input type="checkbox" checked={request.tools.includes(id)} onChange={(event) => toggle(id, event.target.checked)} />
              <span>
                <strong>{label}</strong>
                <span className="faint" style={{ display: "block" }}>{COMPONENT_DETAIL[id]}</span>
              </span>
            </label>
          ))}
          {Object.entries(catalog.core_components).map(([id, label]) => (
            <label className="check check-fixed" key={id} style={{ display: "flex", margin: "0.45rem 0", gap: "0.5rem" }}>
              <input type="checkbox" checked readOnly disabled />
              <span>{label} <span className="faint">· always included</span></span>
            </label>
          ))}
        </Card>

        <Card title="Image size">
          <p style={{ marginTop: 0 }}>
            Estimated image: <strong>{recipe?.estimated_size_gb ?? storage.estimated_image_gb} GB</strong> uncompressed;
            the exported <span className="mono">.tar</span> is about the same size.
          </p>
          <p className="faint">
            CUDA, PyTorch (with cuDNN, cuBLAS and NCCL) and vLLM make this a large image. That is the price of a fully
            offline toolkit: nothing is downloaded when the container starts.
          </p>
          {storage.free_gb != null && (
            <>
              <div className={`size-meter ${storage.enough ? "" : "low"}`}><span style={{ width: `${fill}%` }} /></div>
              <p className="faint" style={{ marginTop: 0 }}>
                Build host: {storage.free_gb} GB free at <span className="mono">{storage.path}</span>,
                about {storage.required_free_gb} GB needed for image layers plus export.
              </p>
            </>
          )}
          {storage.enough === false && (
            <Banner kind="warn">
              Not enough free space on this LayerSmith host for this image. The build will stop before it starts
              pulling. Free space or move the data directory to a larger volume.
            </Banner>
          )}
          <p className="faint">Build time is typically 20–60 minutes: large downloads, and nccl-tests and nvbandwidth are compiled for every GPU generation.</p>
        </Card>
      </div>

      <details className="concepts" style={{ marginTop: "1rem" }}>
        <summary>Internal build mirrors (optional)</summary>
        <p className="faint">For restricted build networks. The container never uses the network at runtime either way.</p>
        <div className="grid two">
          {MIRRORS.map(([key, label]) => (
            <Field label={label} key={key}>
              <input value={request.mirrors?.[key] ?? ""} placeholder="Use upstream default"
                onChange={(event) => setRequest((current) => ({
                  ...current, mirrors: { ...current.mirrors, [key]: event.target.value || undefined },
                }))} />
            </Field>
          ))}
        </div>
      </details>

      <div style={{ marginTop: "1rem" }}><Card title="Build">
        {recipe && (
          <p className="faint">
            CUDA {recipe.cuda_version} · Python {recipe.python_full_version} · PyTorch {recipe.torch}
            {recipe.vllm ? ` · vLLM ${recipe.vllm}` : " · no vLLM"} · nccl-tests {recipe.nccl_tests} · nvbandwidth {recipe.nvbandwidth}
            {` · Textual ${recipe.textual}`}
          </p>
        )}
        <p className="faint">
          After the build LayerSmith exports the image and offers it as <strong>Download Podman Image (.tar)</strong> with
          its SHA-256. On the GPU server: <span className="mono">podman load -i {archive}</span>, then
          <span className="mono"> podman run -it …</span> opens the dashboard.
        </p>
        {containerfile ? <pre className="code">{containerfile}</pre> : <p className="dim">Preview the generated Containerfile before building.</p>}
        <div className="row" style={{ marginTop: "1rem" }}>
          <button onClick={preview} disabled={!resolved?.ok}>Preview Containerfile</button>
          <span className="spacer" />
          <button className="primary" onClick={build} disabled={busy || !resolved?.ok || !validImage}>
            {busy ? "Creating build…" : `Build ${imageRef}`}
          </button>
        </div>
        {!validImage && <p className="warning">Use a lowercase name, a valid repository path and a version tag such as 1.0.0.</p>}
      </Card></div>
    </div>
  );
}
