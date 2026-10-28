import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, OpenShiftCatalog, OpenShiftRequest, OpenShiftResolve } from "../api";
import { Banner, Card, Field } from "../components/ui";
import { CodeBlock } from "../components/Training";

const SAME_AS_CLIENT = "";

export default function OpenShiftWizard({ onLeave }: { onLeave: () => void }) {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<OpenShiftCatalog | null>(null);
  const [request, setRequest] = useState<OpenShiftRequest | null>(null);
  const [resolved, setResolved] = useState<OpenShiftResolve | null>(null);
  const [name, setName] = useState("openshift-admin");
  const [repository, setRepository] = useState("localhost/openshift-admin");
  const [tag, setTag] = useState("1.0.0");
  const [description, setDescription] = useState("");
  const [containerfile, setContainerfile] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.openshiftCatalog().then((value) => {
      setCatalog(value);
      setRequest({
        base: value.default_base, ocp_version: value.default_ocp, versions: {}, optional: [...value.optional],
        timezone: value.defaults.timezone, prod_pattern: value.defaults.prod_pattern,
      });
    }, (exc: Error) => setError(exc.message));
  }, []);

  useEffect(() => {
    if (!request) return;
    let cancelled = false;
    setContainerfile("");
    const timer = setTimeout(() => {
      api.openshiftResolve(request).then((value) => !cancelled && setResolved(value),
        (exc: Error) => !cancelled && setError(exc.message));
    }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [request]);

  if (!catalog || !request) return error ? <Banner>{error}</Banner> : <p className="dim">Loading the OpenShift Admin Toolkit…</p>;

  const recipe = resolved?.ok ? resolved.recipe : undefined;
  const release = catalog.ocp_releases[request.ocp_version];
  const update = (patch: Partial<OpenShiftRequest>) => setRequest({ ...request, ...patch });
  const imageRef = `${repository || name}:${tag}`;
  const alias = `${repository || name}:${release ? request.ocp_version.split(".").slice(0, 2).join(".") : ""}`;
  const archive = `${(repository || name).replace(/^localhost\//, "").replace(/\//g, "-")}-${tag}.tar`;
  const validImage = /^[a-z0-9][a-z0-9-]*$/.test(name)
    && /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,3}$/.test(repository)
    && /^\d+\.\d+\.\d+$/.test(tag);
  const spec = { openshift: request };
  const groups = Array.from(new Set([
    ...Object.values(catalog.tools).map((tool) => tool.group),
    ...Object.values(catalog.package_groups).map((group) => group.group),
  ]));

  function toggle(id: string, on: boolean) {
    update({ optional: on ? [...request!.optional, id] : request!.optional.filter((value) => value !== id) });
  }

  function chooseVersion(id: string, version: string) {
    const versions = { ...request!.versions };
    if (version === SAME_AS_CLIENT) delete versions[id];
    else versions[id] = version;
    update({ versions });
  }

  async function preview() {
    setError(null);
    try {
      const value = await api.preview({ name: name || "openshift-admin", description, template: catalog!.profile.name, spec });
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
        description: description || `${catalog!.profile.name}, OpenShift clients ${request!.ocp_version}`,
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
        <Card title="1. Base image and OpenShift client">
          <Field label="Base image" hint="Enterprise Linux 9 or 10: Red Hat's el9 client builds need glibc 2.34 or newer.">
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
          <Field label="OpenShift client (oc, kubectl)" hint={`From mirror.openshift.com, pinned by sha256 on ${catalog.verified}. Never "latest".`}>
            <select value={request.ocp_version} onChange={(event) => update({ ocp_version: event.target.value })}>
              {Object.entries(catalog.ocp_releases).map(([version, entry]) => (
                <option key={version} value={version}>
                  {version} · {entry.channel} · Kubernetes {entry.kubernetes}{version === catalog.default_ocp ? " (default)" : ""}
                </option>
              ))}
            </select>
          </Field>
          {recipe && <p className="faint">{recipe.compatibility.note}</p>}
        </Card>

        <Card title="2. Environment">
          <div className="grid two">
            <Field label="Time zone" hint="tz database name">
              <input value={request.timezone} onChange={(event) => update({ timezone: event.target.value })} />
            </Field>
            <Field label="Production pattern" hint="Contexts or API servers matching it are marked PROD (regex, | for alternatives).">
              <input value={request.prod_pattern} onChange={(event) => update({ prod_pattern: event.target.value })} />
            </Field>
          </div>
          <dl className="kv">
            <dt>Locale</dt><dd>{catalog.defaults.locale}</dd>
            <dt>Shell</dt><dd>bash, with completion, aliases and a context/namespace prompt</dd>
            <dt>User</dt><dd>none in the image: Distrobox creates yours (rootless)</dd>
            <dt>Services</dt><dd>none: no systemd, no SSH server</dd>
            <dt>Kubeconfig</dt><dd>never in the image: <span className="mono">~/.kube</span> from your home at run time</dd>
          </dl>
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}><Card title="3. Tools">
        <p className="faint" style={{ marginTop: 0 }}>
          Every download is pinned by sha256 and verified during the build; a mismatch fails the build. After the build,
          LayerSmith runs each tool as a non-root user and records the version it reports.
        </p>
        <table className="table">
          <thead><tr><th>Include</th><th>Tool</th><th>Version</th><th>Purpose</th><th>Verified against</th></tr></thead>
          <tbody>
            {groups.map((group) => [
              <tr key={`g-${group}`}><td colSpan={5}><strong>{group}</strong></td></tr>,
              ...Object.entries(catalog.tools).filter(([, tool]) => tool.group === group).map(([id, tool]) => {
                const on = tool.required || request.optional.includes(id);
                const chosen = request.versions[id] ?? (tool.follows_ocp ? SAME_AS_CLIENT : tool.default_version ?? "");
                return (
                  <tr key={id}>
                    <td data-label="Include">
                      <input type="checkbox" checked={on} disabled={tool.required} aria-label={`Include ${tool.label}`}
                        onChange={(event) => toggle(id, event.target.checked)} />
                    </td>
                    <td data-label="Tool">{tool.label}{tool.required && <span className="faint"> · always</span>}</td>
                    <td data-label="Version">
                      {id === "oc" ? <span className="mono">{request.ocp_version}</span> : (
                        <select value={chosen} disabled={!on} onChange={(event) => chooseVersion(id, event.target.value)}>
                          {tool.follows_ocp && <option value={SAME_AS_CLIENT}>= oc {request.ocp_version}</option>}
                          {tool.versions.map((version) => <option key={version} value={version}>{version}</option>)}
                        </select>
                      )}
                    </td>
                    <td data-label="Purpose" className="faint">{tool.purpose}</td>
                    <td data-label="Verified against" className="faint">{tool.verify}</td>
                  </tr>
                );
              }),
              ...Object.entries(catalog.package_groups).filter(([, entry]) => entry.group === group).map(([id, entry]) => (
                <tr key={id}>
                  <td data-label="Include">
                    <input type="checkbox" checked={request.optional.includes(id)} aria-label={`Include ${entry.label}`}
                      onChange={(event) => toggle(id, event.target.checked)} />
                  </td>
                  <td data-label="Tool">{entry.label}</td>
                  <td data-label="Version" className="faint">base repositories</td>
                  <td data-label="Purpose" className="faint">{entry.purpose}</td>
                  <td data-label="Verified against" className="faint">dnf (signed packages)</td>
                </tr>
              )),
            ])}
          </tbody>
        </table>
        <p className="faint">
          Always included: git, jq, curl, wget, openssl, dig/nslookup, nc, traceroute, vim, less, tar, unzip, gzip,
          bash-completion, sudo and everything <span className="mono">distrobox enter</span> needs, so the first start
          installs nothing. No NVIDIA or CUDA drivers: they belong on the GPU nodes.
        </p>
        <details className="concepts">
          <summary>Commands and aliases in the image</summary>
          <table className="table"><tbody>
            {catalog.helpers.map((helper) => (
              <tr key={helper.name}><td data-label="Command" className="mono">{helper.name}</td><td data-label="What" className="faint">{helper.what}</td></tr>
            ))}
            {Object.entries(catalog.aliases).map(([key, value]) => (
              <tr key={key}><td data-label="Alias" className="mono">{key}</td><td data-label="Runs" className="mono faint">{value}</td></tr>
            ))}
          </tbody></table>
        </details>
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
            <dt>Distrobox name</dt><dd className="mono">{alias}</dd>
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
              <dt>Clusters</dt><dd>OpenShift {recipe.compatibility.supported_clusters.join(", ")} (best: {recipe.compatibility.recommended_cluster})</dd>
            </dl>
          )}
          <h4>After the build</h4>
          <CodeBlock code={`podman load -i ${archive}\npodman tag ${imageRef} ${alias}\n\ndistrobox create --name openshift-admin --image ${alias}\ndistrobox enter openshift-admin`} />
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}><Card title="6. Build">
        <p className="faint">
          LayerSmith builds the image, runs two checks inside it with networking disabled (as root: nothing secret in
          any layer; as a non-root user: every tool, completion, the helpers and several kubeconfigs), and exports
          <span className="mono"> {archive}</span> with its SHA-256. The build page shows the log, the installed
          versions, the Distrobox commands and the Assemble manifest.
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
