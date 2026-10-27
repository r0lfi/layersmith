import { Fragment, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AimlCatalog, AimlRequest, AimlResolve, api, CompatRow } from "../api";
import { Banner, Card, Field } from "../components/ui";

const STATUS_ICON: Record<string, string> = { pass: "✓", warn: "!", fail: "✕", unknown: "?" };
const EVIDENCE: Record<string, string> = {
  documented: "publisher documentation", image: "read from this image", hardware: "GPU test on a worker",
};

export function CompatTable({ rows }: { rows: CompatRow[] }) {
  return (
    <div className="checks">
      {rows.map((row) => (
        <div key={row.id} className={`check-row compat ${row.status}`}>
          <div className="compat-line">
            <span className="check-icon" aria-hidden="true">{STATUS_ICON[row.status]}</span>
            <span className="check-label">{row.label}</span>
            <span className="faint compat-detail">
              {row.detail}{" "}
              <span className="compat-evidence">
                Evidence: {EVIDENCE[row.evidence]}
                {row.source && <> · <a href={row.source} target="_blank" rel="noreferrer">source</a></>}
              </span>
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function AiMlWizard({ onLeave }: { onLeave: () => void }) {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<AimlCatalog | null>(null);
  const [request, setRequest] = useState<AimlRequest>({
    template: "nvidia-pytorch", version: "26.09-py3", mode: "custom", tools: [], pip: [],
  });
  const [pipText, setPipText] = useState("");
  const [driver, setDriver] = useState("615.71.09");
  const [gpus, setGpus] = useState<string[]>(["h200-nvl", "rtx-pro-6000-bse"]);
  const [resolved, setResolved] = useState<AimlResolve | null>(null);
  const [name, setName] = useState("");
  const [repository, setRepository] = useState("");
  const [tag, setTag] = useState("1.0.0");
  const [description, setDescription] = useState("");
  const [touchedNames, setTouchedNames] = useState(false);
  const [containerfile, setContainerfile] = useState("");
  const [upstream, setUpstream] = useState<Awaited<ReturnType<typeof api.aimlUpstream>> | null>(null);
  const [checkingUpstream, setCheckingUpstream] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.aimlCatalog().then(setCatalog, (exc: Error) => setError(exc.message));
  }, []);

  const pip = useMemo(() => pipText.split(/[\n,]+/).map((line) => line.trim()).filter(Boolean), [pipText]);
  const fullRequest = useMemo<AimlRequest>(() => ({ ...request, pip }), [request, pip]);

  useEffect(() => {
    let cancelled = false;
    setContainerfile("");
    const timer = setTimeout(() => {
      api.aimlResolve(fullRequest, driver, gpus).then((value) => {
        if (cancelled) return;
        setResolved(value);
        if (value.ok && value.names && !touchedNames) {
          setName(value.names.name);
          setRepository(value.names.repository);
        }
      }, (exc: Error) => !cancelled && setError(exc.message));
    }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [fullRequest, driver, gpus.join(","), touchedNames]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!catalog) return error ? <Banner>{error}</Banner> : <p className="dim">Loading AI/ML templates…</p>;

  const template = catalog.templates[request.template];
  const version = request.version ? template.versions[request.version] : undefined;
  const recipe = resolved?.ok ? resolved.recipe : undefined;
  const custom = template.framework === "custom";
  const spec = { aiml: fullRequest };
  const imageRef = `${repository || name}:${tag}`;
  const archive = `${(repository || name).replace(/^localhost\//, "").replace(/\//g, "-")}-${tag}.tar`;
  const validImage = /^[a-z0-9][a-z0-9-]*$/.test(name)
    && /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,3}$/.test(repository)
    && /^\d+\.\d+\.\d+$/.test(tag);
  const tools = catalog.tools.filter((tool) => !tool.frameworks || tool.frameworks.includes(template.framework));
  const groups = Array.from(new Set(tools.map((tool) => tool.group)));
  const included = recipe?.included ?? version?.included ?? {};

  function chooseTemplate(id: string) {
    const next = catalog!.templates[id];
    setUpstream(null);
    setTouchedNames(false);
    setRequest({
      template: id, version: next.default_version ?? undefined, tools: [], pip: [],
      mode: next.python ? request.mode : "import",
      ...(next.framework === "custom" ? { base: "nvcr.io/nvidia/pytorch:26.09-py3", family: "deb" } : {}),
    });
    setPipText("");
  }

  function toggleTool(id: string, checked: boolean) {
    setRequest((current) => ({
      ...current, tools: checked ? [...current.tools, id] : current.tools.filter((value) => value !== id),
    }));
  }

  async function checkUpstream() {
    setCheckingUpstream(true);
    try {
      setUpstream(await api.aimlUpstream(request.template));
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setCheckingUpstream(false);
    }
  }

  async function preview() {
    setError(null);
    try {
      const value = await api.preview({ name: name || "ai-image", description, template: template.name, spec });
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
      const project = await api.createProject({
        name, description: description || `${template.name} ${recipe?.version ?? ""}`.trim(), repository,
        template: template.name, mode: "gui", spec,
      });
      const result = await api.startBuild(project.id, tag);
      navigate(`/builds/${result.id}`);
    } catch (exc) {
      setError((exc as Error).message);
      setBusy(false);
    }
  }

  const blocking = resolved && !resolved.ok;
  const failing = (resolved?.compatibility ?? []).filter((row) => row.status === "fail");
  const storage = resolved?.storage;

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

      <Card title="1. Template">
        <div className="grid cards">
          {Object.entries(catalog.templates).map(([id, entry]) => (
            <button key={id} className={`tile ${request.template === id ? "selected" : ""}`} onClick={() => chooseTemplate(id)}>
              <div className="tile-title">
                {entry.name}
                {entry.status === "limited" && <span className="badge optional" style={{ marginLeft: "0.4rem" }}>limited</span>}
              </div>
              <div className="tile-sub">{entry.purpose}</div>
            </button>
          ))}
        </div>
        <p className="faint">{template.description} <span className="mono">template v{template.template_version}</span></p>
      </Card>

      <div className="grid two" style={{ marginTop: "1rem" }}>
        <Card title="2. Base image and version">
          {custom ? (
            <>
              <Field label="Base image" hint="Any OCI image with a tag or digest. Its CUDA and framework versions are read from it after the build.">
                <input value={request.base ?? ""} onChange={(event) => setRequest({ ...request, base: event.target.value })} />
              </Field>
              <Field label="Package family">
                <select value={request.family ?? "deb"} onChange={(event) => setRequest({ ...request, family: event.target.value })}>
                  <option value="deb">deb (Ubuntu, Debian)</option>
                  <option value="rpm">rpm (Rocky, Alma, RHEL)</option>
                </select>
              </Field>
            </>
          ) : (
            <>
              <Field label="Version" hint={`Verified against the registry and release notes on ${catalog.verified}.`}>
                <select value={request.version} onChange={(event) => setRequest({ ...request, version: event.target.value, tools: [] })}>
                  {Object.entries(template.versions).map(([key, entry]) => (
                    <option key={key} value={key}>
                      {key} · CUDA {entry.cuda}{entry.framework_version && template.framework !== "cuda" ? ` · ${template.framework === "vllm" ? "vLLM" : template.framework === "tensorflow" ? "TensorFlow" : "PyTorch"} ${entry.framework_version}` : ""}
                    </option>
                  ))}
                </select>
              </Field>
              {version && (
                <dl className="kv">
                  <dt>Image</dt><dd className="mono">{version.image}:{version.tag}</dd>
                  <dt>Digest</dt><dd className="mono truncate-long" title={version.digest}>{version.digest}</dd>
                  <dt>CUDA</dt><dd>{version.cuda}</dd>
                  {version.torch && <><dt>PyTorch</dt><dd>{version.torch}</dd></>}
                  {template.framework !== "cuda" && <><dt>Framework</dt><dd>{version.framework_version}</dd></>}
                  <dt>Python</dt><dd>{version.python ?? (template.python ? "read after the build" : "none")}</dd>
                  <dt>OS</dt><dd>{version.os ?? "read after the build"}</dd>
                  {Object.entries(version.components).map(([key, value]) => (
                    <Fragment key={key}><dt>{key}</dt><dd>{value}</dd></Fragment>
                  ))}
                  <dt>GPUs</dt><dd>compute capability {version.min_capability}+{version.max_native_capability ? `, native kernels up to ${version.max_native_capability}` : ""}</dd>
                  <dt>Download</dt><dd>{version.compressed_gb} GB compressed</dd>
                </dl>
              )}
              <div className="row" style={{ marginTop: "0.6rem" }}>
                <button onClick={checkUpstream} disabled={checkingUpstream}>
                  {checkingUpstream ? "Asking the registry…" : "Show tags in the registry"}
                </button>
              </div>
              {upstream && (
                <div style={{ marginTop: "0.5rem" }}>
                  {upstream.error ? <p className="warning">{upstream.error}</p> : (
                    <p className="faint">
                      {upstream.upstream.map((entry) => (
                        <span key={entry.tag} className="mono" style={{ marginRight: "0.7rem" }}>
                          {entry.tag}{entry.verified ? " ✓" : ""}
                        </span>
                      ))}
                      <br />✓ verified by LayerSmith. Other tags exist upstream but are not verified here; build them
                      with the Custom AI Container.
                    </p>
                  )}
                </div>
              )}
            </>
          )}
          <h4>How</h4>
          <label className="check" style={{ display: "flex", gap: "0.5rem", margin: "0.4rem 0" }}>
            <input type="radio" checked={request.mode === "import"} onChange={() => setRequest({ ...request, mode: "import", tools: [] })} />
            <span><strong>Import unchanged</strong> <span className="faint">— the publisher's layers byte for byte, plus LayerSmith labels</span></span>
          </label>
          <label className="check" style={{ display: "flex", gap: "0.5rem", margin: "0.4rem 0" }}>
            <input type="radio" checked={request.mode === "custom"} disabled={!template.python}
              onChange={() => setRequest({ ...request, mode: "custom" })} />
            <span><strong>Build a custom image</strong> <span className="faint">— add Python tools through a generated Containerfile{!template.python && " (needs Python; not for CUDA images)"}</span></span>
          </label>
        </Card>

        <Card title="3. AI tools">
          {request.mode === "import" ? (
            <p className="faint">An import adds nothing. Choose “Build a custom image” to add tools.</p>
          ) : (
            <>
              <p className="faint" style={{ marginTop: 0 }}>
                Nothing is installed unless you tick it. What the base image already has is shown and kept:
                LayerSmith never lets pip replace the base image's PyTorch, CUDA libraries or other framework builds.
              </p>
              {groups.map((group) => (
                <div key={group}>
                  <h4>{group}</h4>
                  {tools.filter((tool) => tool.group === group).map((tool) => {
                    const have = included[tool.pip];
                    return (
                      <label key={tool.id} className={`check ${have ? "check-fixed" : ""}`} style={{ display: "flex", gap: "0.5rem", margin: "0.35rem 0" }}>
                        <input type="checkbox" checked={!!have || request.tools.includes(tool.id)} disabled={!!have}
                          onChange={(event) => toggleTool(tool.id, event.target.checked)} />
                        <span>
                          {tool.label}{" "}
                          {have ? <span className="faint">· in the base image ({have})</span> : null}
                          {tool.note && request.tools.includes(tool.id) && <span className="faint" style={{ display: "block" }}>{tool.note}</span>}
                        </span>
                      </label>
                    );
                  })}
                </div>
              ))}
              <Field label="Other Python packages" hint="One per line: name, name==1.2.3 or name>=1.2,<2. No URLs, paths or pip options.">
                <textarea rows={3} value={pipText} onChange={(event) => setPipText(event.target.value)} placeholder="einops==0.8.1" />
              </Field>
              <details className="concepts">
                <summary>Package index (optional)</summary>
                <Field label="Index URL" hint="An internal PyPI mirror for the build host. Credentials are refused: they would end up in the image history.">
                  <input value={request.pip_index_url ?? ""} placeholder="https://pypi.org/simple"
                    onChange={(event) => setRequest({ ...request, pip_index_url: event.target.value || undefined })} />
                </Field>
              </details>
              {Object.keys(included).length > 0 && (
                <details className="concepts">
                  <summary>Already in the base image ({version?.package_count ?? Object.keys(included).length} Python packages)</summary>
                  <p className="faint mono" style={{ fontSize: "0.78rem" }}>
                    {Object.entries(included).map(([key, value]) => `${key} ${value}`).join(" · ")}
                  </p>
                </details>
              )}
            </>
          )}
        </Card>
      </div>

      <div className="grid two" style={{ marginTop: "1rem" }}>
        <Card title="4. Name and tag">
          <div className="grid two">
            <Field label="Image name (project)"><input value={name} onChange={(event) => { setTouchedNames(true); setName(event.target.value); }} /></Field>
            <Field label="Tag (version)"><input value={tag} onChange={(event) => setTag(event.target.value)} /></Field>
          </div>
          <Field label="Repository" hint="localhost/… keeps the name Podman shows after podman load.">
            <input value={repository} onChange={(event) => { setTouchedNames(true); setRepository(event.target.value); }} />
          </Field>
          <Field label="Description"><input value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
          <dl className="kv">
            <dt>Image</dt><dd className="mono">{imageRef}</dd>
            <dt>Archive</dt><dd className="mono">{archive}</dd>
          </dl>
          {!validImage && <p className="warning">Use a lowercase name, a valid repository path and a version tag such as 1.0.0.</p>}
        </Card>

        <Card title="5. Validate">
          {blocking && <Banner>{resolved!.error}</Banner>}
          {recipe && recipe.warnings.map((warning) => <Banner key={warning} kind="warn">{warning}</Banner>)}
          {recipe && (
            <p className="faint" style={{ marginTop: 0 }}>
              {recipe.mode === "import" ? "Import" : `Install: ${recipe.install.length ? recipe.install.join(", ") : "nothing"}`}
              {recipe.estimated_size_gb && ` · about ${recipe.estimated_size_gb} GB unpacked`}
            </p>
          )}
          <div className="grid two">
            <Field label="Target NVIDIA driver"><input value={driver} onChange={(event) => setDriver(event.target.value)} /></Field>
            <Field label="Target GPUs">
              <div>
                {Object.entries(catalog.gpus).map(([id, gpu]) => (
                  <label key={id} className="check" style={{ display: "flex", gap: "0.4rem" }}>
                    <input type="checkbox" checked={gpus.includes(id)}
                      onChange={(event) => setGpus((current) => event.target.checked ? [...current, id] : current.filter((value) => value !== id))} />
                    <span>{gpu.name}</span>
                  </label>
                ))}
              </div>
            </Field>
          </div>
          {resolved?.compatibility && <CompatTable rows={resolved.compatibility} />}
          <p className="faint">
            Before the build this rests on documentation. After it, the basic test reads the GPU architectures the
            image's kernels were compiled for; a GPU test on the worker is the final proof.
          </p>
          {storage && storage.enough === false && (
            <Banner kind="warn">
              {storage.free_gb} GB free at <span className="mono">{storage.path}</span>, about {storage.required_free_gb} GB
              needed. The build will stop before it pulls.
            </Banner>
          )}
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}><Card title="6. Build">
        <p className="faint">
          {request.mode === "import" ? "LayerSmith pulls the base by digest and tags it" : "LayerSmith builds the image"},
          runs the basic test inside it with networking disabled, and exports <span className="mono">{archive}</span> with
          its SHA-256. The build page then follows the log, shows the image's metadata, runs exports and generates Slurm
          batch scripts.
        </p>
        {failing.length > 0 && <Banner kind="warn">Compatibility check failed for: {failing.map((row) => row.label).join(", ")}. You can still build.</Banner>}
        {containerfile ? <pre className="code">{containerfile}</pre> : <p className="dim">Preview the generated Containerfile before building.</p>}
        <div className="row" style={{ marginTop: "1rem" }}>
          <button onClick={preview} disabled={!resolved?.ok}>Preview Containerfile</button>
          <span className="spacer" />
          <button className="primary" onClick={build} disabled={busy || !resolved?.ok || !validImage}>
            {busy ? "Creating build…" : request.mode === "import" ? `Import as ${imageRef}` : `Build ${imageRef}`}
          </button>
        </div>
      </Card></div>
    </div>
  );
}
