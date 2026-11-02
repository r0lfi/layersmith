import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, OpencodeCatalog, OpencodeRequest, OpencodeResolve } from "../api";
import { Banner, Card, Field } from "../components/ui";
import { CodeBlock } from "../components/Training";

const DIAGRAM = `host                                shared container "opencode" (always running)
$ cd ~/projects/app
$ opencode  --sudo-->  opencode-enter  --exec as you-->  opencode
                                                         your uid and groups, in ~/projects/app
/home is shared at the same path; /etc/opencode holds the site settings (your LLM endpoint)`;

export default function OpencodeWizard({ onLeave }: { onLeave: () => void }) {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<OpencodeCatalog | null>(null);
  const [request, setRequest] = useState<OpencodeRequest | null>(null);
  const [resolved, setResolved] = useState<OpencodeResolve | null>(null);
  const [name, setName] = useState("opencode");
  const [repository, setRepository] = useState("localhost/opencode");
  const [tag, setTag] = useState("1.0.0");
  const [description, setDescription] = useState("");
  const [containerfile, setContainerfile] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.opencodeCatalog().then((value) => {
      setCatalog(value);
      setRequest({ base: value.default_base, opencode_version: value.default_version, cpu: value.default_cpu,
                   optional: [...value.default_optional] });
    }, (exc: Error) => setError(exc.message));
  }, []);

  useEffect(() => {
    if (!request) return;
    let cancelled = false;
    setContainerfile("");
    const timer = setTimeout(() => {
      api.opencodeResolve(request).then((value) => !cancelled && setResolved(value),
        (exc: Error) => !cancelled && setError(exc.message));
    }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [request]);

  if (!catalog || !request) return error ? <Banner>{error}</Banner> : <p className="dim">Loading the opencode Shared Terminal…</p>;

  const recipe = resolved?.ok ? resolved.recipe : undefined;
  const update = (patch: Partial<OpencodeRequest>) => setRequest({ ...request, ...patch });
  const imageRef = `${repository || name}:${tag}`;
  const archive = `${(repository || name).replace(/^localhost\//, "").replace(/\//g, "-")}-${tag}.tar`;
  const validImage = /^[a-z0-9][a-z0-9-]*$/.test(name)
    && /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,3}$/.test(repository)
    && /^\d+\.\d+\.\d+$/.test(tag);
  const spec = { opencode: request };

  function toggle(id: string, on: boolean) {
    update({ optional: on ? [...request!.optional, id] : request!.optional.filter((value) => value !== id) });
  }

  async function preview() {
    setError(null);
    try {
      const value = await api.preview({ name: name || "opencode", description, template: catalog!.profile.name, spec });
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
        name, repository, template: catalog!.profile.name, mode: "gui", spec,
        description: description || `${catalog!.profile.name}, opencode ${request!.opencode_version}`,
      });
      const result = await api.startBuild(project.id, tag);
      navigate(`/builds/${result.id}`);
    } catch (exc) {
      setError((exc as Error).message);
      setBusy(false);
    }
  }

  return (
    <div style={{ marginTop: "1rem" }}>
      <div className="row" style={{ marginBottom: "1rem" }}>
        <div>
          <h2 style={{ margin: 0 }}>{catalog.profile.name}</h2>
          <p className="dim" style={{ marginBottom: 0 }}>{catalog.profile.description}</p>
          <p className="faint" style={{ marginTop: "0.2rem" }}>{catalog.profile.category} · profile v{catalog.profile.version}</p>
        </div>
        <span className="spacer" />
        <button onClick={onLeave}>Back to general images</button>
      </div>
      {error && <Banner>{error}</Banner>}

      <div className="grid two">
        <Card title="1. Base image and opencode">
          <Field label="Base image" hint="Enterprise Linux 9 or 10; toolchains come from its own repositories.">
            <select value={request.base} onChange={(event) => update({ base: event.target.value })}>
              {Object.keys(catalog.bases).map((key) => (
                <option key={key} value={key}>{key}{key === catalog.default_base ? " (default)" : ""}</option>
              ))}
              <option value="custom">Custom EL-compatible base (e.g. an internal mirror)</option>
            </select>
          </Field>
          {request.base === "custom" ? (
            <div className="grid two">
              <Field label="Base reference" hint="With a tag or digest.">
                <input value={request.base_reference ?? ""} placeholder="registry.example.com/almalinux:10"
                  onChange={(event) => update({ base_reference: event.target.value })} />
              </Field>
              <Field label="Enterprise Linux major">
                <select value={request.base_el ?? ""} onChange={(event) => update({ base_el: event.target.value })}>
                  <option value="">choose</option><option value="10">10</option><option value="9">9</option>
                </select>
              </Field>
            </div>
          ) : (
            <p className="faint mono" style={{ marginTop: 0 }}>{catalog.bases[request.base]?.source} · linux/amd64</p>
          )}
          <div className="grid two">
            <Field label="opencode version" hint={`GitHub release, pinned by sha256 on ${catalog.verified}. Never "latest".`}>
              <select value={request.opencode_version} onChange={(event) => update({ opencode_version: event.target.value })}>
                {catalog.versions.map((version) => (
                  <option key={version} value={version}>{version}{version === catalog.default_version ? " (default)" : ""}</option>
                ))}
              </select>
            </Field>
            <Field label="Target CPU" hint="The baseline build runs on any x86-64 CPU.">
              <select value={request.cpu} onChange={(event) => update({ cpu: event.target.value })}>
                {Object.entries(catalog.cpu_variants).map(([id, variant]) => (
                  <option key={id} value={id}>{variant.label}</option>
                ))}
              </select>
            </Field>
          </div>
        </Card>

        <Card title="2. How it runs">
          <pre className="code">{DIAGRAM}</pre>
          <dl className="kv">
            <dt>Container</dt><dd>one per host, always running (systemd unit <span className="mono">opencode-container</span>)</dd>
            <dt>Users</dt><dd>type <span className="mono">opencode</span> on the host; several at once, each as themselves</dd>
            <dt>Access</dt><dd>members of the <span className="mono">{catalog.host.group}</span> group (a sudo rule for one helper)</dd>
            <dt>Shared</dt><dd><span className="mono">{catalog.host.mounts}</span> at the same path (configurable)</dd>
            <dt>LLM endpoint</dt><dd>you write it in <span className="mono">/etc/opencode/opencode.json</span> on the host</dd>
            <dt>Privileges</dt><dd>none: no capabilities, no sudo inside, no-new-privileges</dd>
          </dl>
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}><Card title="3. Tools">
        <p className="faint" style={{ marginTop: 0 }}>
          The agent can only run what is in the image: add the toolchains your projects need to build and test.
        </p>
        <table className="table">
          <thead><tr><th>Include</th><th>Tool</th><th>Version</th><th>Purpose</th></tr></thead>
          <tbody>
            <tr>
              <td data-label="Include"><input type="checkbox" checked disabled aria-label="opencode (always)" /></td>
              <td data-label="Tool">opencode<span className="faint"> · always</span></td>
              <td data-label="Version" className="mono">{request.opencode_version}</td>
              <td data-label="Purpose" className="faint">The terminal coding agent (one self-contained binary)</td>
            </tr>
            <tr>
              <td data-label="Include"><input type="checkbox" checked disabled aria-label="ripgrep (always)" /></td>
              <td data-label="Tool">ripgrep (rg)<span className="faint"> · always</span></td>
              <td data-label="Version" className="mono">{catalog.ripgrep}</td>
              <td data-label="Purpose" className="faint">Code search; opencode would otherwise download it at first use</td>
            </tr>
            {catalog.optional.map((id) => {
              const group = catalog.package_groups[id];
              return (
                <tr key={id}>
                  <td data-label="Include">
                    <input type="checkbox" checked={request.optional.includes(id)} aria-label={`Include ${group.label}`}
                      onChange={(event) => toggle(id, event.target.checked)} />
                  </td>
                  <td data-label="Tool">{group.label}</td>
                  <td data-label="Version" className="faint">base repositories</td>
                  <td data-label="Purpose" className="faint">{group.purpose}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="faint">
          Always included: bash, git, jq, vim, less, diff/patch, tar, gzip, xz, zip/unzip, file, tree and the ssh client.
          Set in the image so opencode never reaches for the internet:{" "}
          {Object.keys(catalog.environment).filter((key) => key.includes("DISABLE")).map((key) => (
            <span key={key} className="mono">{key} </span>
          ))}
        </p>
      </Card></div>

      <div className="grid two" style={{ marginTop: "1rem" }}>
        <Card title="4. Name and tag">
          <div className="grid two">
            <Field label="Image name (project)"><input value={name} onChange={(event) => setName(event.target.value)} /></Field>
            <Field label="Tag (version)"><input value={tag} onChange={(event) => setTag(event.target.value)} /></Field>
          </div>
          <Field label="Repository" hint="localhost/… keeps the name Podman shows after podman load.">
            <input value={repository} onChange={(event) => setRepository(event.target.value)} />
          </Field>
          <Field label="Description"><input value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
          <dl className="kv">
            <dt>Image</dt><dd className="mono">{imageRef}</dd>
            <dt>Archive</dt><dd className="mono">{archive}</dd>
          </dl>
          {!validImage && <p className="warning">Use a lowercase name, a valid repository path and a version tag such as 1.0.0.</p>}
        </Card>

        <Card title="5. Validate">
          {resolved && !resolved.ok && <Banner>{resolved.error}</Banner>}
          {recipe?.warnings.map((warning) => <Banner key={warning} kind="warn">{warning}</Banner>)}
          {recipe && (
            <dl className="kv">
              <dt>Downloads</dt><dd>{recipe.tools.filter((t) => t.enabled && t.kind === "download").map((t) => `${t.label} ${t.version}`).join(", ")}</dd>
              <dt>Packages</dt><dd>{recipe.packages.length} from the base's repositories, all updated at build time</dd>
              <dt>Size</dt><dd>about {recipe.estimated_size_gb} GB unpacked</dd>
            </dl>
          )}
          <h4>On the host, after the build</h4>
          <CodeBlock code={`sudo bash install-host.sh --archive ${archive}\nsudo usermod -aG opencode alice\n\n# as alice, in a project:\nopencode`} />
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}><Card title="6. Build">
        <p className="faint">
          LayerSmith builds the image, runs two checks inside it with networking disabled (as root: nothing secret, the
          offline settings, ensure-user; as a non-root user: every tool, opencode's configuration, a local model entry and
          code search), and exports <span className="mono">{archive}</span> with its SHA-256. The build page has the archive,
          <span className="mono"> install-host.sh</span> and the README, with Download all.
        </p>
        {containerfile ? <pre className="code">{containerfile}</pre> : <p className="dim">Preview the generated Containerfile before building.</p>}
        <div className="row" style={{ marginTop: "1rem" }}>
          <button onClick={preview} disabled={!resolved?.ok}>Preview Containerfile</button>
          <span className="spacer" />
          <button className="primary" onClick={build} disabled={busy || !resolved?.ok || !validImage}>
            {busy ? "Creating build…" : `Build ${imageRef}`}
          </button>
        </div>
      </Card></div>
    </div>
  );
}
