import { useState } from "react";
import { api, CheckRow, ExportContents, GuideSection, GuideStep, TrainingDetail, TrainingTool } from "../api";
import { Banner, Card } from "./ui";

/** Code with a copy button. Clipboard access needs HTTPS or localhost; the text stays selectable regardless. */
export function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="code-wrap">
      <pre className="code">{code}</pre>
      <button
        className="ghost copy"
        onClick={() =>
          navigator.clipboard?.writeText(code).then(
            () => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            },
            () => undefined,
          )
        }
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

/** One tool: the essentials on the card, the rest behind "Details". */
export function ToolCard({
  tool,
  selected,
  onToggle,
  note,
}: {
  tool: TrainingTool;
  selected?: boolean;
  onToggle?: () => void;
  note?: string;
}) {
  return (
    <div className={`tool-card ${selected ? "selected" : ""}`}>
      <div className="tool-head">
        {onToggle && (
          <input type="checkbox" checked={!!selected} onChange={onToggle} aria-label={tool.name} />
        )}
        <strong>{tool.name}</strong>
        {tool.version && <span className="mono faint">{tool.version}</span>}
        <span className="spacer" />
        <span className={`badge ${tool.required ? "required" : "optional"}`}>{tool.required ? "Required" : "Optional"}</span>
      </div>
      <p className="tool-what">{tool.what}</p>
      <p className="faint tool-use">{tool.used_for}</p>
      {tool.required && tool.why && (
        <p className="tool-why">
          <span className="faint">Why it is included: </span>
          {tool.why}
        </p>
      )}
      {note && <p className="tool-why warn-text">{note}</p>}
      <details>
        <summary>Details</summary>
        <dl className="facts">
          <dt>When it is useful</dt>
          <dd>{tool.when}</dd>
          <dt>Pulls in</dt>
          <dd>{tool.pulls_in}</dd>
          <dt>Limitations</dt>
          <dd>{tool.limitations}</dd>
          <dt>Version</dt>
          <dd className="mono">{tool.version ?? "-"}</dd>
          {tool.docs && (
            <>
              <dt>Documentation</dt>
              <dd>
                <a href={tool.docs} target="_blank" rel="noreferrer">
                  {tool.docs.replace(/^https?:\/\//, "")}
                </a>
              </dd>
            </>
          )}
        </dl>
      </details>
    </div>
  );
}

const STATUS_ICON: Record<string, string> = { passed: "✓", failed: "✕", not_run: "–" };

export function CheckList({ rows }: { rows: CheckRow[] }) {
  return (
    <div className="checks">
      {rows.map((row) => (
        <details key={row.id} className={`check-row ${row.status}`}>
          <summary>
            <span className="check-icon" aria-hidden="true">{STATUS_ICON[row.status]}</span>
            <span className="check-label">{row.label}</span>
            <span className="check-summary faint">{row.summary}</span>
          </summary>
          <div className="check-body">
            <p className="faint">{row.meaning} Applies to {row.applies_to}.</p>
            {row.steps && row.steps.length > 0 && (
              <table className="table">
                <tbody>
                  {row.steps.map((step, index) => (
                    <tr key={index}>
                      <td data-label="Step">{step.name}</td>
                      <td data-label="Result" className={step.status === "failed" ? "warn-text" : ""}>
                        {step.status}
                        {step.seconds !== undefined && step.seconds !== null ? ` (${step.seconds}s)` : ""}
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

function EngineCommands({ step, engine }: { step: GuideStep; engine: "docker" | "podman" }) {
  return (
    <>
      {step.code && <CodeBlock code={step.code} />}
      {step.docker && step.podman && <CodeBlock code={engine === "docker" ? step.docker : step.podman} />}
    </>
  );
}

export function GettingStarted({ sections, image }: { sections: GuideSection[]; image: string }) {
  const [engine, setEngine] = useState<"docker" | "podman">("docker");
  return (
    <Card
      title="Getting started"
      actions={
        <div className="seg" role="tablist" aria-label="Container engine">
          {(["docker", "podman"] as const).map((name) => (
            <button key={name} className={engine === name ? "on" : ""} onClick={() => setEngine(name)}>
              {name === "docker" ? "Docker" : "Podman"}
            </button>
          ))}
        </div>
      }
    >
      <p className="faint" style={{ marginTop: 0 }}>
        Commands for <span className="mono">{image}</span>. Run them on the machine that will use the image.
      </p>
      {sections.map((section) => (
        <details key={section.id} className="guide" open={["dirs", "gpu", "shell", "examples"].includes(section.id)}>
          <summary>{section.title}</summary>
          <div className="guide-body">
            {section.text && <p>{section.text}</p>}
            {section.table && (
              <table className="table">
                <thead>
                  <tr><th>In the container</th><th>On the host</th><th>Purpose</th></tr>
                </thead>
                <tbody>
                  {section.table.map((row) => (
                    <tr key={row.path}>
                      <td data-label="Container" className="mono">{row.path}</td>
                      <td data-label="Host" className="mono">$LLM/{row.host} ({row.mode})</td>
                      <td data-label="Purpose">{row.purpose}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <EngineCommands step={section} engine={engine} />
            {section.steps?.map((step) => (
              <div key={step.title} className="guide-step">
                <h4>{step.title}</h4>
                {step.text && <p className="faint">{step.text}</p>}
                <EngineCommands step={step} engine={engine} />
              </div>
            ))}
            {section.note && <p className="faint note">{section.note}</p>}
            {section.link && (
              <p>
                <a href={section.link} target="_blank" rel="noreferrer">Documentation</a>
              </p>
            )}
          </div>
        </details>
      ))}
    </Card>
  );
}

export function ExportList({ contents }: { contents: ExportContents }) {
  return (
    <div className="export-list">
      <p>{contents.summary}</p>
      <div className="grid two">
        <div>
          <h4>Included</h4>
          <ul>{contents.included.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
        <div>
          <h4>Bring separately</h4>
          <ul>{contents.external.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
      </div>
    </div>
  );
}

/** Everything a finished training build shows on its page. */
export function TrainingPanel({ buildId, detail, ready, onChange }: {
  buildId: string;
  detail: TrainingDetail;
  ready: boolean;
  onChange: (detail: TrainingDetail) => void;
}) {
  const recipe = detail.recipe;
  const gpu = detail.checks.find((row) => row.id === "gpu");
  const [report, setReport] = useState("");
  const [engine, setEngine] = useState<"docker" | "podman">("docker");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      onChange(await api.gpuReport(buildId, report));
      setReport("");
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Card title={`LLM training template: ${recipe.profile_name}`}>
        {detail.customized && (
          <Banner kind="warn">
            This image adds packages, files or scripts to the standard recipe. The checks below ran on this image;
            results of the standard recipe do not carry over to it.
          </Banner>
        )}
        <div className="grid two">
          <table className="table">
            <tbody>
              <tr><td data-label="Profile">Profile</td><td>{recipe.profile_name} {recipe.profile_version}</td></tr>
              <tr><td data-label="Target">Target</td><td>{recipe.target_name}</td></tr>
              <tr><td data-label="Stack">Stack</td><td>{recipe.stack_name}</td></tr>
              <tr><td data-label="Python">Python</td><td className="mono">{recipe.python}</td></tr>
              {recipe.torch && (
                <tr><td data-label="PyTorch">PyTorch</td><td className="mono">{recipe.versions.torch} (CUDA {recipe.cuda})</td></tr>
              )}
              {recipe.driver && <tr><td data-label="Driver">Driver</td><td>{recipe.driver}</td></tr>}
              <tr><td data-label="Add-ons">Add-ons</td><td>{recipe.addons.join(", ") || "none"}</td></tr>
            </tbody>
          </table>
          <div>
            <h3>Checks</h3>
            <CheckList rows={detail.checks} />
          </div>
        </div>

        {ready && gpu && gpu.command && (
          <div className="gpu-report">
            <div className="row">
              <h3>GPU test on the target machine</h3>
              <span className="spacer" />
              <div className="seg">
                {(["docker", "podman"] as const).map((name) => (
                  <button key={name} className={engine === name ? "on" : ""} onClick={() => setEngine(name)}>
                    {name === "docker" ? "Docker" : "Podman"}
                  </button>
                ))}
              </div>
            </div>
            <p className="faint">
              LayerSmith does not run GPU tests: the build host's GPU says nothing about the machine that will train.
              Run this there, then paste the JSON it prints to record the result for this image.
            </p>
            <CodeBlock code={engine === "docker" ? gpu.command.docker : gpu.command.podman} />
            <textarea
              value={report}
              onChange={(event) => setReport(event.target.value)}
              placeholder='{"check": "gpu", "status": "passed", ...}'
              style={{ minHeight: "90px" }}
            />
            {error && <Banner>{error}</Banner>}
            <div className="row end" style={{ marginTop: "0.5rem" }}>
              <button className="primary" onClick={submit} disabled={busy || !report.trim()}>
                {busy ? "Recording…" : "Record GPU result"}
              </button>
            </div>
          </div>
        )}
      </Card>
      {ready && (
        <Card title="What the export contains">
          <ExportList contents={detail.export_contents} />
        </Card>
      )}
      {ready && <GettingStarted sections={detail.getting_started.sections} image={detail.getting_started.image} />}
    </>
  );
}
