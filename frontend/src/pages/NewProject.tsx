import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, Catalog, Spec } from "../api";
import { Banner, Card, Field, useLoad } from "../components/ui";

const STEPS = ["Purpose", "Base", "Version", "Tools", "Packages", "Files", "Scripts", "Image", "Review"] as const;

interface Draft {
  template: string;
  distribution: string;
  version: string;
  customSource: string;
  customFamily: string;
  presets: string[];
  packages: string[];
  extraPackages: string;
  files: { sha256: string; filename: string; destination: string }[];
  scripts: { pre_build: string; post_install: string; entrypoint: string };
  name: string;
  repository: string;
  imageVersion: string;
  description: string;
  architecture: string;
}

const EMPTY: Draft = {
  template: "Developer",
  distribution: "AlmaLinux",
  version: "",
  customSource: "",
  customFamily: "rpm",
  presets: [],
  packages: [],
  extraPackages: "",
  files: [],
  scripts: { pre_build: "", post_install: "", entrypoint: "" },
  name: "",
  repository: "",
  imageVersion: "1.0.0",
  description: "",
  architecture: "amd64",
};

function buildSpec(draft: Draft, catalog: Catalog): Spec {
  const custom = draft.distribution === "Custom";
  return {
    base: custom
      ? { source: draft.customSource, family: draft.customFamily }
      : { distribution: draft.distribution, version: draft.version },
    architecture: draft.architecture,
    presets: draft.presets,
    packages: draft.packages,
    extra_packages: draft.extraPackages.split(/[\s,]+/).filter(Boolean),
    files: draft.files.map((file) => ({ sha256: file.sha256, destination: file.destination })),
    scripts: Object.fromEntries(Object.entries(draft.scripts).filter(([, body]) => body.trim())),
    tests: catalog.templates.find((t) => t.name === draft.template)?.spec.tests ?? [],
  };
}

export default function NewProject() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { data: catalog, error: catalogError } = useLoad(() => api.catalog());
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<Draft>({ ...EMPTY, template: params.get("template") ?? EMPTY.template });
  const [preview, setPreview] = useState<{ containerfile: string; packages: string[]; warnings: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const update = (patch: Partial<Draft>) => setDraft((current) => ({ ...current, ...patch }));

  // Selecting a template seeds its presets; the user can still change them.
  useEffect(() => {
    if (!catalog) return;
    const template = catalog.templates.find((entry) => entry.name === draft.template);
    update({ presets: template?.spec.presets ?? [] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog, draft.template]);

  useEffect(() => {
    if (!catalog || draft.distribution === "Custom") return;
    const distribution = catalog.distributions.find((entry) => entry.name === draft.distribution);
    if (distribution && !distribution.versions.includes(draft.version)) {
      update({ version: distribution.versions[0] });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog, draft.distribution]);

  const spec = useMemo(() => (catalog ? buildSpec(draft, catalog) : null), [draft, catalog]);

  // Refresh the preview when the review step opens.
  useEffect(() => {
    if (step !== STEPS.length - 1 || !spec) return;
    setError(null);
    api
      .preview({ name: draft.name || "image", description: draft.description, template: draft.template, spec })
      .then(setPreview)
      .catch((exc: Error) => {
        setPreview(null);
        setError(exc.message);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  if (catalogError) return <Banner>{catalogError}</Banner>;
  if (!catalog) return <p className="dim">Loading…</p>;

  const distribution = catalog.distributions.find((entry) => entry.name === draft.distribution);

  async function create() {
    if (!spec) return;
    setBusy(true);
    setError(null);
    try {
      const project = await api.createProject({
        name: draft.name,
        description: draft.description,
        repository: draft.repository || undefined,
        template: draft.template,
        mode: "gui",
        spec,
      });
      const build = await api.startBuild(project.id, draft.imageVersion);
      navigate(`/builds/${build.id}`);
    } catch (exc) {
      setError((exc as Error).message);
      setBusy(false);
    }
  }

  const canAdvance = () => {
    if (step === 1 && draft.distribution === "Custom") return draft.customSource.trim().length > 0;
    if (step === 7) return /^[a-z0-9][a-z0-9-]*$/.test(draft.name) && /^\d+\.\d+\.\d+$/.test(draft.imageVersion);
    return true;
  };

  return (
    <>
      <h1>Create image</h1>
      <div className="steps" style={{ marginTop: "1rem" }}>
        {STEPS.map((label, index) => (
          <span
            key={label}
            className={`step-chip ${index === step ? "current" : index < step ? "done" : ""}`}
          >
            {index + 1}. {label}
          </span>
        ))}
      </div>

      {error && <Banner>{error}</Banner>}

      <Card title={`Step ${step + 1} of ${STEPS.length}: ${STEPS[step]}`}>
        {step === 0 && (
          <div className="grid cards">
            {catalog.templates.map((template) => (
              <button
                key={template.name}
                className={`tile ${draft.template === template.name ? "selected" : ""}`}
                onClick={() => update({ template: template.name })}
              >
                <div className="tile-title">{template.name}</div>
                <div className="tile-sub">{template.description}</div>
              </button>
            ))}
          </div>
        )}

        {step === 1 && (
          <>
            <div className="grid cards">
              {[...catalog.distributions.map((entry) => entry.name), "Custom"].map((name) => (
                <button
                  key={name}
                  className={`tile ${draft.distribution === name ? "selected" : ""}`}
                  onClick={() => update({ distribution: name })}
                >
                  <div className="tile-title">{name}</div>
                  <div className="tile-sub">
                    {name === "Custom"
                      ? "Any OCI image"
                      : catalog.families[catalog.distributions.find((d) => d.name === name)!.family]}
                  </div>
                </button>
              ))}
            </div>
            {draft.distribution === "Custom" && (
              <div style={{ marginTop: "1rem" }}>
                <Field label="Base image" hint="For example registry.example.com/team/base:1.2">
                  <input
                    value={draft.customSource}
                    onChange={(event) => update({ customSource: event.target.value })}
                    placeholder="registry.example.com/team/base:1.2"
                  />
                </Field>
                <Field label="Package manager" hint="LayerSmith cannot detect this from an arbitrary image.">
                  <select value={draft.customFamily} onChange={(e) => update({ customFamily: e.target.value })}>
                    {Object.entries(catalog.families).map(([family, manager]) => (
                      <option key={family} value={family}>{manager} ({family})</option>
                    ))}
                  </select>
                </Field>
              </div>
            )}
          </>
        )}

        {step === 2 && (
          <>
            {distribution ? (
              <Field label={`${draft.distribution} version`}>
                <select value={draft.version} onChange={(event) => update({ version: event.target.value })}>
                  {distribution.versions.map((version) => (
                    <option key={version} value={version}>
                      {draft.distribution} {version} — {distribution.sources[version]}
                    </option>
                  ))}
                </select>
              </Field>
            ) : (
              <p className="dim">Using {draft.customSource || "a custom image"}.</p>
            )}
            <Field label="Architecture">
              <select value={draft.architecture} onChange={(event) => update({ architecture: event.target.value })}>
                {catalog.architectures.map((arch) => (
                  <option key={arch} value={arch}>{arch}</option>
                ))}
              </select>
            </Field>
          </>
        )}

        {step === 3 && (
          <>
            <p className="faint" style={{ marginTop: 0 }}>
              Tool sets from the template are preselected. Package names are resolved per distribution.
            </p>
            <div className="grid two">
              {catalog.presets.map((preset) => (
                <label key={preset.name} className="check">
                  <input
                    type="checkbox"
                    checked={draft.presets.includes(preset.name)}
                    onChange={(event) =>
                      update({
                        presets: event.target.checked
                          ? [...draft.presets, preset.name]
                          : draft.presets.filter((name) => name !== preset.name),
                      })
                    }
                  />
                  <span>
                    <strong>{preset.name}</strong>
                    <span className="faint"> — {preset.description}</span>
                  </span>
                </label>
              ))}
            </div>

            <h3 style={{ marginTop: "1.2rem" }}>Individual tools</h3>
            {catalog.categories.map((category) => (
              <div key={category.name} style={{ marginTop: "0.7rem" }}>
                <div className="faint" style={{ fontSize: "0.82rem" }}>{category.name}</div>
                <div className="check-grid">
                  {category.packages.map((name) => (
                    <label key={name} className="check">
                      <input
                        type="checkbox"
                        checked={draft.packages.includes(name)}
                        onChange={(event) =>
                          update({
                            packages: event.target.checked
                              ? [...draft.packages, name]
                              : draft.packages.filter((item) => item !== name),
                          })
                        }
                      />
                      <span className="mono">{name}</span>
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </>
        )}

        {step === 4 && (
          <Field
            label="Additional packages"
            hint="Exact package names for the chosen distribution, separated by spaces or commas."
          >
            <input
              value={draft.extraPackages}
              onChange={(event) => update({ extraPackages: event.target.value })}
              placeholder="htop ncdu"
            />
          </Field>
        )}

        {step === 5 && (
          <>
            <p className="faint" style={{ marginTop: 0 }}>
              Files are uploaded now and copied into the image at build time.
            </p>
            <input
              type="file"
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                try {
                  const uploaded = await api.upload(file);
                  update({
                    files: [
                      ...draft.files,
                      { sha256: uploaded.sha256, filename: uploaded.filename, destination: `/opt/${uploaded.filename}` },
                    ],
                  });
                } catch (exc) {
                  setError((exc as Error).message);
                }
                event.target.value = "";
              }}
            />
            {draft.files.map((file, index) => (
              <div className="row" key={file.sha256} style={{ marginTop: "0.7rem" }}>
                <span className="mono truncate">{file.filename}</span>
                <input
                  value={file.destination}
                  onChange={(event) => {
                    const files = [...draft.files];
                    files[index] = { ...file, destination: event.target.value };
                    update({ files });
                  }}
                  style={{ maxWidth: "22rem" }}
                />
                <button
                  className="ghost"
                  onClick={() => update({ files: draft.files.filter((item) => item.sha256 !== file.sha256) })}
                >
                  Remove
                </button>
              </div>
            ))}
          </>
        )}

        {step === 6 && (
          <>
            {(["pre_build", "post_install", "entrypoint"] as const).map((key) => (
              <Field
                key={key}
                label={key.replace("_", " ")}
                hint={
                  key === "pre_build"
                    ? "Runs before packages are installed."
                    : key === "post_install"
                    ? "Runs after packages are installed."
                    : "Becomes the image entrypoint."
                }
              >
                <textarea
                  value={draft.scripts[key]}
                  onChange={(event) => update({ scripts: { ...draft.scripts, [key]: event.target.value } })}
                  placeholder={key === "entrypoint" ? 'exec "$@"' : "# shell commands"}
                  style={{ minHeight: "110px" }}
                />
              </Field>
            ))}
          </>
        )}

        {step === 7 && (
          <>
            <Field label="Image name" hint="Lowercase letters, digits and dashes.">
              <input
                value={draft.name}
                onChange={(event) => update({ name: event.target.value })}
                placeholder="network-tools"
              />
            </Field>
            <Field label="Repository" hint="Defaults to the configured namespace plus the image name.">
              <input
                value={draft.repository}
                onChange={(event) => update({ repository: event.target.value })}
                placeholder="layersmith/network-tools"
              />
            </Field>
            <Field label="Version" hint="Versions are immutable: a version is built once.">
              <input value={draft.imageVersion} onChange={(event) => update({ imageVersion: event.target.value })} />
            </Field>
            <Field label="Description">
              <input value={draft.description} onChange={(event) => update({ description: event.target.value })} />
            </Field>
          </>
        )}

        {step === 8 && (
          <>
            <div className="grid two">
              <div>
                <h3>Summary</h3>
                <table className="table">
                  <tbody>
                    <tr><td data-label="Template">Template</td><td>{draft.template}</td></tr>
                    <tr>
                      <td data-label="Base">Base</td>
                      <td className="mono">
                        {draft.distribution === "Custom"
                          ? draft.customSource
                          : `${draft.distribution} ${draft.version}`}
                      </td>
                    </tr>
                    <tr><td data-label="Architecture">Architecture</td><td className="mono">{draft.architecture}</td></tr>
                    <tr><td data-label="Image">Image</td><td className="mono">{draft.repository || `layersmith/${draft.name}`}:{draft.imageVersion}</td></tr>
                    <tr><td data-label="Packages">Packages</td><td>{preview?.packages.length ?? 0}</td></tr>
                    <tr><td data-label="Files">Files</td><td>{draft.files.length}</td></tr>
                  </tbody>
                </table>
                {preview?.packages.length ? (
                  <p className="mono faint" style={{ wordBreak: "break-word" }}>{preview.packages.join(" ")}</p>
                ) : null}
              </div>
              <div>
                <h3>Containerfile</h3>
                <pre className="code">{preview?.containerfile ?? "Generating…"}</pre>
              </div>
            </div>
            {preview?.warnings.map((warning) => (
              <Banner kind="warn" key={warning}>{warning}</Banner>
            ))}
          </>
        )}

        <div className="row end" style={{ marginTop: "1.2rem" }}>
          <button className="ghost" onClick={() => setStep((value) => Math.max(0, value - 1))} disabled={step === 0}>
            Back
          </button>
          {step < STEPS.length - 1 ? (
            <button className="primary" onClick={() => setStep((value) => value + 1)} disabled={!canAdvance()}>
              Next
            </button>
          ) : (
            <button className="primary" onClick={create} disabled={busy || !preview}>
              {busy ? "Starting…" : "Build image"}
            </button>
          )}
        </div>
      </Card>
    </>
  );
}
