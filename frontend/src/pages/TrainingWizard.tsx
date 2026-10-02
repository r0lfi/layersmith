import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, TrainingCatalog, TrainingRequest, TrainingResolve } from "../api";
import { Banner, Card, Field } from "../components/ui";
import { CodeBlock, ExportList, ToolCard } from "../components/Training";

const STEPS = ["Purpose", "Hardware", "Software", "Add-ons", "Image", "Review"] as const;

interface Draft {
  intent: Record<string, string>;
  profile: string;
  target: string;
  stack: string;
  addons: string[];
  extraPython: string;
  extraPackages: string;
  name: string;
  repository: string;
  imageVersion: string;
  description: string;
}

function request(draft: Draft): TrainingRequest {
  return {
    profile: draft.profile,
    target: draft.target || undefined,
    stack: draft.stack || undefined,
    addons: draft.addons,
    intent: draft.intent,
    extra_python: draft.extraPython.split(/[\s,]+/).filter(Boolean),
  };
}

export default function TrainingWizard({ onLeave }: { onLeave: () => void }) {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<TrainingCatalog | null>(null);
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<Draft>({
    intent: { task: "sft", method: "lora", execution: "single-gpu" },
    profile: "hf-finetune",
    target: "",
    stack: "",
    addons: ["tensorboard"],
    extraPython: "",
    extraPackages: "",
    name: "",
    repository: "",
    imageVersion: "1.0.0",
    description: "",
  });
  const [resolved, setResolved] = useState<TrainingResolve | null>(null);
  const [preview, setPreview] = useState<{ containerfile: string; packages: string[]; warnings: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const update = (patch: Partial<Draft>) => setDraft((current) => ({ ...current, ...patch }));

  useEffect(() => {
    api.trainingCatalog().then(setCatalog, (exc: Error) => setError(exc.message));
  }, []);

  // The backend decides compatibility; the page only shows what it says.
  useEffect(() => {
    let cancelled = false;
    api.trainingResolve(request(draft)).then(
      (result) => !cancelled && setResolved(result),
      (exc: Error) => !cancelled && setError(exc.message),
    );
    return () => {
      cancelled = true;
    };
  }, [draft.profile, draft.target, draft.stack, draft.addons, draft.extraPython, draft.intent]);

  const profile = catalog?.profiles.find((entry) => entry.id === draft.profile);
  // Keep showing the last working recipe while a conflict is on screen, so the
  // choice that caused it stays visible and can be undone.
  const [lastRecipe, setLastRecipe] = useState<TrainingResolve["recipe"]>(undefined);
  useEffect(() => {
    if (resolved?.ok && resolved.recipe) setLastRecipe(resolved.recipe);
  }, [resolved]);
  const recipe = resolved?.ok ? resolved.recipe : lastRecipe;
  const spec = useMemo(
    () => ({
      training: request(draft),
      extra_packages: draft.extraPackages.split(/[\s,]+/).filter(Boolean),
    }),
    [draft],
  );

  useEffect(() => {
    if (step !== STEPS.length - 1) return;
    setPreview(null);
    api
      .preview({ name: draft.name || "training-image", description: draft.description, template: profile?.name, spec })
      .then(setPreview, (exc: Error) => setError(exc.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  if (!catalog) return error ? <Banner>{error}</Banner> : <p className="dim">Loading…</p>;

  function chooseProfile(id: string) {
    const chosen = catalog!.profiles.find((entry) => entry.id === id)!;
    update({ profile: id, target: "", stack: "", addons: chosen.default_addons });
  }

  function applyAlternative() {
    const alternative = resolved?.alternative;
    if (!alternative) return;
    if (alternative.profile && alternative.profile !== draft.profile) {
      const chosen = catalog!.profiles.find((entry) => entry.id === alternative.profile)!;
      update({ profile: chosen.id, target: alternative.target ?? "", stack: "", addons: chosen.default_addons });
      return;
    }
    update({
      ...(alternative.target !== undefined ? { target: alternative.target } : {}),
      ...(alternative.stack !== undefined ? { stack: alternative.stack } : {}),
      ...(alternative.addons !== undefined ? { addons: alternative.addons } : {}),
      ...(alternative.extra_python !== undefined ? { extraPython: alternative.extra_python.join(" ") } : {}),
    });
  }

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const project = await api.createProject({
        name: draft.name,
        description: draft.description,
        repository: draft.repository || undefined,
        template: `LLM: ${profile?.name ?? draft.profile}`,
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

  const recommendation = resolved?.recommendation;
  const canAdvance =
    step === 4
      ? /^[a-z0-9][a-z0-9-]*$/.test(draft.name) && /^\d+\.\d+\.\d+$/.test(draft.imageVersion)
      : step >= 1
      ? !!resolved?.ok
      : true;
  const optional = recipe?.tools.filter((tool) => !tool.required) ?? [];

  return (
    <>
      <div className="steps" style={{ marginTop: "1rem" }}>
        {STEPS.map((label, index) => (
          <span key={label} className={`step-chip ${index === step ? "current" : index < step ? "done" : ""}`}>
            {index + 1}. {label}
          </span>
        ))}
      </div>

      {error && <Banner>{error}</Banner>}
      {resolved && !resolved.ok && step > 0 && (
        <Banner kind="warn">
          <p style={{ margin: 0 }}>{resolved.error}</p>
          {resolved.alternative && (
            <button style={{ marginTop: "0.6rem" }} onClick={applyAlternative}>
              {resolved.label ?? "Use the working alternative"}
            </button>
          )}
        </Banner>
      )}

      <Card title={`Step ${step + 1} of ${STEPS.length}: ${STEPS[step]}`}>
        {step === 0 && (
          <>
            <p style={{ marginTop: 0 }}>{catalog.category.intro}</p>
            <details className="concepts">
              <summary>Basic concepts: fine-tuning, SFT, LoRA, QLoRA, DPO, checkpoints…</summary>
              <dl className="facts">
                {catalog.concepts.map((concept) => (
                  <div key={concept.id}>
                    <dt>{concept.term}</dt>
                    <dd>{concept.text}</dd>
                  </div>
                ))}
              </dl>
            </details>

            <h3 style={{ marginTop: "1.2rem" }}>What is the environment for?</h3>
            <p className="faint" style={{ marginTop: "0.2rem" }}>
              Three separate questions: the task says what the model learns, the method says which weights change,
              and execution says where it runs. SFT with LoRA is one combination, not two alternatives.
            </p>
            <div className="intent-grid">
              {Object.entries(catalog.intents).map(([key, group]) => (
                <div key={key}>
                  <div className="intent-label">{group.label}</div>
                  <div className="faint intent-hint">{group.hint}</div>
                  <div className="chips">
                    {group.options.map((option) => (
                      <button
                        key={option.id}
                        className={`chip ${draft.intent[key] === option.id ? "on" : ""}`}
                        onClick={() => update({ intent: { ...draft.intent, [key]: option.id } })}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {recommendation && (
              <Banner kind="info">
                Suggested for this purpose:{" "}
                <strong>{catalog.profiles.find((p) => p.id === recommendation.profile)?.name}</strong> -{" "}
                {recommendation.reason}
                {recommendation.profile !== draft.profile && (
                  <button style={{ marginLeft: "0.6rem" }} onClick={() => chooseProfile(recommendation.profile)}>
                    Choose it
                  </button>
                )}
              </Banner>
            )}

            <h3>Profile</h3>
            <div className="grid cards" style={{ marginTop: "0.6rem" }}>
              {catalog.profiles.map((entry) => (
                <button
                  key={entry.id}
                  className={`tile profile-tile ${draft.profile === entry.id ? "selected" : ""}`}
                  onClick={() => chooseProfile(entry.id)}
                >
                  <div className="row" style={{ gap: "0.4rem" }}>
                    <span className="tile-title">{entry.name}</span>
                    {entry.recommended && <span className="badge recommended">Recommended</span>}
                  </div>
                  <div className="tile-sub">{entry.summary}</div>
                  <ul className="tile-list">
                    {entry.best_for.slice(0, 3).map((item) => <li key={item}>{item}</li>)}
                  </ul>
                  <div className="tile-foot faint">
                    {entry.targets.map((t) => catalog.targets[t].name).join(" / ")}
                  </div>
                </button>
              ))}
            </div>
            {profile && <p className="profile-description">{profile.description}</p>}
            <p className="faint" style={{ fontSize: "0.82rem" }}>
              Not what you need? <button className="link" onClick={onLeave}>Build a general-purpose image instead</button>.
            </p>
          </>
        )}

        {step === 1 && profile && (
          <>
            <h3>Hardware target</h3>
            <div className="grid cards" style={{ marginTop: "0.6rem" }}>
              {Object.entries(catalog.targets).map(([id, target]) => {
                const supported = profile.targets.includes(id);
                const chosen = (draft.target || profile.targets[0]) === id;
                return (
                  <button
                    key={id}
                    className={`tile ${chosen ? "selected" : ""}`}
                    onClick={() => update({ target: id })}
                    data-unsupported={supported ? undefined : "true"}
                  >
                    <div className="tile-title">{target.name}</div>
                    <div className="tile-sub">{target.summary}</div>
                    {!supported && <div className="tile-foot warn-text">Not a target of {profile.name}</div>}
                  </button>
                );
              })}
            </div>
            <p className="faint" style={{ fontSize: "0.85rem" }}>
              Architecture: <span className="mono">linux/amd64</span>. Other platforms get their own documented and
              verified recipe before they are offered.
            </p>
            {recipe && (
              <>
                <h4>The machine that runs the image needs</h4>
                <ul>{recipe.host_requirements.map((item) => <li key={item}>{item}</li>)}</ul>
                {recipe.driver && <p className="faint">{recipe.driver} {recipe.gpu_support}</p>}
                <p className="faint">
                  Building does not need a GPU: LayerSmith builds and checks the image on CPU. GPU details of the
                  build host are never used as facts about the training machine.
                </p>
              </>
            )}
            {profile.stacks.length > 1 && (
              <>
                <h3 style={{ marginTop: "1rem" }}>Software stack</h3>
                <div className="grid cards" style={{ marginTop: "0.6rem" }}>
                  {profile.stacks.map((id) => {
                    const stack = catalog.stacks[id];
                    return (
                      <button
                        key={id}
                        className={`tile ${(draft.stack || profile.stacks[0]) === id ? "selected" : ""}`}
                        onClick={() => update({ stack: id })}
                      >
                        <div className="tile-title">{stack.name}</div>
                        <div className="tile-sub">{stack.summary}</div>
                        <div className="tile-foot faint">{stack.driver}</div>
                      </button>
                    );
                  })}
                </div>
              </>
            )}
          </>
        )}

        {step === 2 && recipe && (
          <>
            <table className="table">
              <tbody>
                <tr>
                  <td data-label="Base image">Base image</td>
                  <td>
                    <span className="mono">{recipe.base.display}</span>
                    <div className="mono faint truncate-long" title={recipe.base.digest}>{recipe.base.digest}</div>
                    <div className="faint">{recipe.base.reason}</div>
                  </td>
                </tr>
                <tr><td data-label="Architecture">Architecture</td><td className="mono">linux/{recipe.architecture}</td></tr>
                <tr><td data-label="Python">Python</td><td className="mono">{recipe.python}</td></tr>
                {recipe.torch && (
                  <tr>
                    <td data-label="PyTorch">PyTorch</td>
                    <td><span className="mono">{recipe.versions.torch}</span> <span className="faint">({recipe.gpu_support})</span></td>
                  </tr>
                )}
                <tr>
                  <td data-label="Locked">Locked</td>
                  <td>
                    {Object.keys(recipe.versions).length} Python packages, hash-checked
                    <span className="mono faint"> {recipe.lock_digest.slice(0, 19)}…</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <h3 style={{ marginTop: "1rem" }}>Included tools</h3>
            <p className="faint" style={{ marginTop: "0.2rem" }}>
              Chosen automatically for this profile. Versions are the ones the build installs.
            </p>
            <div className="grid cards">
              {recipe.tools.filter((tool) => tool.required).map((tool) => <ToolCard key={tool.id} tool={tool} />)}
            </div>
            <details style={{ marginTop: "1rem" }}>
              <summary>System packages ({recipe.system_packages.length})</summary>
              <p className="mono faint">{recipe.system_packages.join(" ")}</p>
              <p className="faint">{recipe.system_why}</p>
            </details>
            <details>
              <summary>All locked Python packages ({Object.keys(recipe.versions).length})</summary>
              <p className="mono faint" style={{ wordBreak: "break-word" }}>
                {Object.entries(recipe.versions).map(([name, version]) => `${name}==${version}`).join("  ")}
              </p>
            </details>
          </>
        )}

        {step === 3 && profile && (
          <>
            <p className="faint" style={{ marginTop: 0 }}>
              Only add-ons with a working recipe for this profile are listed. Selecting one can change the base image
              or stack; the reason is shown below.
            </p>
            <div className="grid cards">
              {optional.map((tool) => {
                const addon = catalog.addons[tool.addon!];
                const otherStack = addon.stacks && !addon.stacks.includes(recipe?.stack ?? draft.stack);
                return (
                  <ToolCard
                    key={tool.id}
                    tool={tool}
                    selected={draft.addons.includes(tool.addon!)}
                    onToggle={() =>
                      update({
                        addons: draft.addons.includes(tool.addon!)
                          ? draft.addons.filter((a) => a !== tool.addon)
                          : [...draft.addons, tool.addon!],
                      })
                    }
                    note={otherStack ? addon.summary : undefined}
                  />
                );
              })}
              {optional.length === 0 && <p className="faint">This profile has no optional add-ons.</p>}
            </div>
            {recipe?.notes.map((note) => <Banner kind="info" key={note}>{note}</Banner>)}

            <details style={{ marginTop: "1rem" }}>
              <summary>Own packages (makes this a modified recipe)</summary>
              <Field
                label="Extra Python packages"
                hint="Pinned exactly, name==version, separated by spaces. Installed after the locked stack; pip check must still pass."
              >
                <input value={draft.extraPython} onChange={(event) => update({ extraPython: event.target.value })}
                       placeholder="einops==0.8.2" />
              </Field>
              <Field label="Extra Ubuntu packages" hint="Exact package names, separated by spaces.">
                <input value={draft.extraPackages} onChange={(event) => update({ extraPackages: event.target.value })}
                       placeholder="htop tmux" />
              </Field>
              <p className="faint">
                Earlier test results of the standard recipe do not apply to a modified image; its own build runs the
                checks again.
              </p>
            </details>
          </>
        )}

        {step === 4 && (
          <>
            <Field label="Image name" hint="Lowercase letters, digits and dashes.">
              <input value={draft.name} onChange={(event) => update({ name: event.target.value })} placeholder="llm-finetune" />
            </Field>
            <Field label="Repository" hint="Defaults to the configured namespace plus the image name.">
              <input value={draft.repository} onChange={(event) => update({ repository: event.target.value })}
                     placeholder="layersmith/llm-finetune" />
            </Field>
            <Field label="Version" hint="Versions are immutable: a version is built once.">
              <input value={draft.imageVersion} onChange={(event) => update({ imageVersion: event.target.value })} />
            </Field>
            <Field label="Description">
              <input value={draft.description} onChange={(event) => update({ description: event.target.value })} />
            </Field>
          </>
        )}

        {step === 5 && recipe && (
          <>
            <Banner kind="info">{resolved?.before_build}</Banner>
            <div className="grid two">
              <div>
                <h3>Summary</h3>
                <table className="table">
                  <tbody>
                    <tr><td data-label="Profile">Profile</td><td>{recipe.profile_name} {recipe.profile_version}</td></tr>
                    <tr><td data-label="Target">Target</td><td>{recipe.target_name}</td></tr>
                    <tr><td data-label="Stack">Stack</td><td>{recipe.stack_name}</td></tr>
                    <tr>
                      <td data-label="Base">Base</td>
                      <td>
                        <span className="mono">{recipe.base.display}</span>
                        <div className="mono faint truncate-long" title={recipe.base.digest}>{recipe.base.digest}</div>
                      </td>
                    </tr>
                    <tr><td data-label="Architecture">Architecture</td><td className="mono">linux/{recipe.architecture}</td></tr>
                    <tr><td data-label="Python">Python</td><td className="mono">{recipe.python}</td></tr>
                    {recipe.torch && (
                      <tr><td data-label="PyTorch">PyTorch</td><td className="mono">{recipe.versions.torch}</td></tr>
                    )}
                    <tr><td data-label="Add-ons">Add-ons</td><td>{recipe.addons.join(", ") || "none"}</td></tr>
                    <tr>
                      <td data-label="Image">Image</td>
                      <td className="mono">{draft.repository || `layersmith/${draft.name}`}:{draft.imageVersion}</td>
                    </tr>
                  </tbody>
                </table>
                <h4>Versions</h4>
                <p className="mono faint" style={{ wordBreak: "break-word" }}>
                  {recipe.tools
                    .filter((tool) => tool.required || recipe.addons.includes(tool.addon ?? ""))
                    .map((tool) => `${tool.name} ${tool.version ?? ""}`)
                    .join(" · ")}
                </p>
                <h4>The training machine needs</h4>
                <ul>{recipe.host_requirements.map((item) => <li key={item}>{item}</li>)}</ul>
                {resolved?.export_contents && (
                  <>
                    <h4>The export will contain</h4>
                    <ExportList contents={resolved.export_contents} />
                  </>
                )}
                <p className="faint">
                  After the build LayerSmith runs three checks in the image without network: dependencies, a CPU smoke
                  test and the offline example. Image size and build time are measured and shown on the build page.
                </p>
                {recipe.notes.map((note) => <Banner kind="info" key={note}>{note}</Banner>)}
              </div>
              <div>
                <h3>Containerfile</h3>
                {preview ? <CodeBlock code={preview.containerfile} /> : <pre className="code">Generating…</pre>}
              </div>
            </div>
            {preview?.warnings.map((warning) => <Banner kind="warn" key={warning}>{warning}</Banner>)}
          </>
        )}

        <div className="row end" style={{ marginTop: "1.2rem" }}>
          <button className="ghost" onClick={() => (step === 0 ? onLeave() : setStep((value) => value - 1))}>
            Back
          </button>
          {step < STEPS.length - 1 ? (
            <button className="primary" onClick={() => setStep((value) => value + 1)} disabled={!canAdvance}>
              Next
            </button>
          ) : (
            <button className="primary" onClick={create} disabled={busy || !preview || !resolved?.ok}>
              {busy ? "Starting…" : "Build image"}
            </button>
          )}
        </div>
      </Card>
    </>
  );
}
