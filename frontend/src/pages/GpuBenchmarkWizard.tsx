import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, GpuBenchmarkCatalog, GpuBenchmarkRequest, GpuBenchmarkResolve } from "../api";
import { Banner, Card, Field } from "../components/ui";

const NAME = "gpu-benchmark";
const TEMPLATE = "NVIDIA GPU Benchmark Toolkit";

export default function GpuBenchmarkWizard({ onLeave }: { onLeave: () => void }) {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<GpuBenchmarkCatalog | null>(null);
  const [request, setRequest] = useState<GpuBenchmarkRequest>({
    cuda_version: "13.0.2", python_version: "3.12", tools: ["llm", "compute", "memory", "nccl"],
  });
  const [name, setName] = useState(NAME);
  const [repository, setRepository] = useState("layersmith/gpu-benchmark");
  const [tag, setTag] = useState("1.0.0");
  const [description, setDescription] = useState(
    "NVIDIA GPU benchmarking toolkit for LLM inference, CUDA compute, memory bandwidth and multi-node NCCL performance.",
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
  const imageRef = `${repository || `layersmith/${name || NAME}`}:${tag}`;
  const validImage = /^[a-z0-9][a-z0-9-]*$/.test(name)
    && /^[a-z0-9][a-z0-9._/-]*$/.test(repository || `layersmith/${name}`)
    && /^\d+\.\d+\.\d+$/.test(tag);

  async function preview() {
    setError(null);
    try {
      const value = await api.preview({ name: name || NAME, description, template: TEMPLATE, spec });
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

  const selectedCuda = catalog.cuda_variants[request.cuda_version];
  return (
    <div style={{ marginTop: "1rem" }}>
      <div className="row" style={{ marginBottom: "1rem" }}>
        <div><h2 style={{ margin: 0 }}>{catalog.category.template}</h2><p className="dim">{catalog.category.intro}</p></div>
        <span className="spacer" />
        <button onClick={onLeave}>Back to general images</button>
      </div>
      {error && <Banner>{error}</Banner>}
      {resolved && !resolved.ok && <Banner kind="warn">{resolved.error}</Banner>}
      <div className="grid two">
        <Card title="CUDA and Python">
          <Field label="CUDA development image">
            <select value={request.cuda_version} onChange={(event) => setRequest({ ...request, cuda_version: event.target.value })}>
              {Object.entries(catalog.cuda_variants).map(([version, variant]) => (
                <option key={version} value={version}>{version} · {variant.base}</option>
              ))}
            </select>
          </Field>
          <Field label="Python version">
            <select value={request.python_version} onChange={(event) => setRequest({ ...request, python_version: event.target.value })}>
              {Object.entries(catalog.python_versions).map(([version, exact]) => (
                <option key={version} value={version}>{version} (pinned {exact})</option>
              ))}
            </select>
          </Field>
          <p className="faint">The host supplies the NVIDIA driver. The image includes the CUDA user-space toolkit and does not install kernel drivers.</p>
          <p className="faint">This recipe currently builds for Linux amd64; the host can run AlmaLinux, RHEL, or another supported Linux distribution.</p>
          {selectedCuda && <p className="mono faint">Base: {selectedCuda.base} · PyTorch {resolved?.recipe?.torch ?? selectedCuda.torch}</p>}
        </Card>
        <Card title="Benchmark components">
          {Object.entries(catalog.tool_groups).map(([id, label]) => (
            <label className="check" key={id} style={{ display: "flex", margin: "0.65rem 0" }}>
              <input type="checkbox" checked={request.tools.includes(id)}
                onChange={(event) => setRequest((current) => ({
                  ...current,
                  tools: event.target.checked
                    ? [...current.tools, id]
                    : current.tools.filter((tool) => tool !== id),
                }))} />
              <span>{label}</span>
            </label>
          ))}
          <p className="faint">vLLM is available in the pinned CUDA 13.0.2 recipe. CUDA 12.8.2 can be selected for non-LLM benchmark groups.</p>
        </Card>
      </div>
      <details className="concepts" style={{ marginTop: "1rem" }}>
        <summary>Internal build mirrors (optional)</summary>
        <p className="faint">Set internal package, Python and source mirrors for restricted or offline build networks.</p>
        <div className="grid two">
          {([
            ["apt_main", "Ubuntu APT mirror"],
            ["apt_security", "Ubuntu security mirror"],
            ["pypi_index", "Python package index"],
            ["torch_index", "PyTorch wheel index"],
            ["python_mirror", "CPython distribution mirror"],
            ["cuda_samples_repository", "CUDA Samples Git mirror"],
            ["nccl_tests_repository", "nccl-tests Git mirror"],
          ] as const).map(([key, label]) => (
            <Field label={label} key={key}>
              <input value={request.mirrors?.[key] ?? ""} placeholder="Use upstream default"
                onChange={(event) => setRequest((current) => ({
                  ...current, mirrors: { ...current.mirrors, [key]: event.target.value || undefined },
                }))} />
            </Field>
          ))}
        </div>
      </details>
      <div style={{ marginTop: "1rem" }}><Card title="Image name and tag">
        <div className="grid two">
          <Field label="Project name"><input value={name} onChange={(event) => setName(event.target.value)} /></Field>
          <Field label="Image repository"><input value={repository} onChange={(event) => setRepository(event.target.value)} placeholder={`layersmith/${name || NAME}`} /></Field>
          <Field label="Image tag"><input value={tag} onChange={(event) => setTag(event.target.value)} /></Field>
          <Field label="Description"><input value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
        </div>
        <p className="mono faint">{imageRef}</p>
      </Card></div>
      <div style={{ marginTop: "1rem" }}><Card title="Resolved recipe and build preview">
        {resolved?.recipe && (
          <p className="faint">
            CUDA {resolved.recipe.cuda_version} · Python {resolved.recipe.python_full_version} · PyTorch {resolved.recipe.torch}
            {resolved.recipe.vllm ? ` · vLLM ${resolved.recipe.vllm}` : " · vLLM not selected"}
            {` · nccl-tests ${resolved.recipe.nccl_tests}`}
          </p>
        )}
        {containerfile ? <pre className="code">{containerfile}</pre> : <p className="dim">Preview the generated Containerfile before building.</p>}
        <div className="row" style={{ marginTop: "1rem" }}>
          <button onClick={preview} disabled={!resolved?.ok}>Preview Containerfile</button>
          <span className="spacer" />
          <button className="primary" onClick={build} disabled={busy || !resolved?.ok || !validImage}>
            {busy ? "Creating build…" : `Create and build ${imageRef}`}
          </button>
        </div>
        {!validImage && <p className="warning">Use a lowercase project name, a valid repository path, and a semantic version tag such as 1.0.0.</p>}
      </Card></div>
    </div>
  );
}
