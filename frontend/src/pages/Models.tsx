import { FormEvent, Fragment, useCallback, useState } from "react";
import { api, BuildStatus, CatalogModel, DownloadFile, formatDecimalBytes, LlmModel } from "../api";
import { Banner, Card, Empty, StatusPill, useLoad, usePoll } from "../components/ui";

const ACTIVE_MODEL = ["queued", "downloading", "verifying"];

function Progress({ model }: { model: LlmModel }) {
  const percent = model.total_bytes ? Math.floor((model.done_bytes / model.total_bytes) * 100) : 0;
  return (
    <div>
      <div className="progress" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
        <span style={{ width: `${percent}%` }} />
      </div>
      <span className="faint" style={{ fontSize: "0.8rem" }}>
        {model.status === "verifying" ? "verifying checksums…"
          : `${formatDecimalBytes(model.done_bytes)} of ${formatDecimalBytes(model.total_bytes)} · ${percent}%`}
      </span>
    </div>
  );
}

function ModelFiles({ model }: { model: LlmModel }) {
  const { data, error } = useLoad(() => api.modelFiles(model.id), [model.id, model.part_count]);
  if (error) return <Banner>{error}</Banner>;
  if (!data) return <p className="dim">Loading…</p>;
  const script = data.files.find((file) => file.name.startsWith("download-"));
  const parts = data.files.filter((file) => file.kind === "part");
  return (
    <div style={{ marginTop: "0.6rem" }}>
      <p className="faint" style={{ marginTop: 0 }}>
        {parts.length} part{parts.length === 1 ? "" : "s"} under 1 GB plus an install script. On a machine that
        can reach LayerSmith, <span className="mono">{script?.name}</span> fetches and verifies them all; on the
        GPU host <span className="mono">bash {data.install.replace("./", "")}</span> puts the model in{" "}
        <span className="mono">/opt/models/{model.name}</span>. For the GPU benchmark image, the build page's
        GPU kit bundles models and image together.
      </p>
      <div className="row" style={{ gap: "0.5rem", flexWrap: "wrap", marginBottom: "0.6rem" }}>
        {script && <a className="button primary" href={script.url}>{script.name}</a>}
        {data.files.filter((file) => file.kind !== "part").map((file) => (
          <a key={file.name} className="button" href={file.url}>{file.name}</a>
        ))}
      </div>
      <FileTable files={parts} />
    </div>
  );
}

export function FileTable({ files }: { files: DownloadFile[] }) {
  return (
    <table className="table">
      <thead><tr><th>File</th><th>Size</th><th>SHA-256</th><th></th></tr></thead>
      <tbody>
        {files.map((file) => (
          <tr key={file.name}>
            <td data-label="File" className="mono">{file.name}</td>
            <td data-label="Size" style={{ whiteSpace: "nowrap" }}>{formatDecimalBytes(file.size)}</td>
            <td data-label="SHA-256" className="mono faint split-sha" title={file.sha256 ?? ""}>
              {file.sha256 ?? (file.kind === "script" ? "script" : "checksum list")}
            </td>
            <td><a className="button" href={file.url}>Download</a></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function Models() {
  const catalog = useLoad(() => api.llmCatalog(), []);
  const models = useLoad(() => api.llmModels(), []);
  const [message, setMessage] = useState<{ error: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [repo, setRepo] = useState("");
  const [revision, setRevision] = useState("main");

  const refresh = useCallback(() => {
    catalog.reload();
    models.reload();
  }, [catalog, models]);
  usePoll(refresh, Boolean(models.data?.some((model) => ACTIVE_MODEL.includes(model.status))), 2000);

  async function act(key: string, action: () => Promise<unknown>, done?: string) {
    setBusy(key);
    setMessage(null);
    try {
      await action();
      if (done) setMessage({ error: false, text: done });
      refresh();
    } catch (exc) {
      setMessage({ error: true, text: (exc as Error).message });
    } finally {
      setBusy(null);
    }
  }

  function fetchCatalog(entry: CatalogModel) {
    act(entry.key, () => api.requestModel({ repo_id: entry.repo_id, catalog_key: entry.key }),
      `${entry.repo_id}: download started.`);
  }

  function fetchCustom(event: FormEvent) {
    event.preventDefault();
    act("custom", () => api.requestModel({ repo_id: repo.trim(), revision: revision.trim() || "main" }),
      `${repo.trim()}: download started.`).then(() => setRepo(""));
  }

  function remove(model: LlmModel) {
    if (!window.confirm(`Delete ${model.name} (${formatDecimalBytes(model.total_bytes)}) from LayerSmith?\n\n` +
      "The downloaded files are removed. Models already installed on GPU hosts are not affected.")) return;
    act(model.id, () => api.deleteModel(model.id), `${model.name} deleted.`);
  }

  if (catalog.error) return <Banner>{catalog.error}</Banner>;
  if (!catalog.data || !models.data) return <p className="dim">Loading…</p>;
  const data = catalog.data;
  const purposes = Object.entries(data.purposes);

  return (
    <>
      <div className="row">
        <div>
          <h1>Models</h1>
          <p className="dim">
            Open LLMs from Hugging Face for the GPU benchmark, downloaded once and handed over in parts under
            1 GB. Stored in <span className="mono">{data.model_dir}</span>
            {data.free_bytes !== null && <> · {formatDecimalBytes(data.free_bytes)} free</>}.
          </p>
        </div>
        <span className="spacer" />
        <button className="ghost" onClick={refresh}>Refresh</button>
      </div>

      {message && (message.error ? <Banner>{message.text}</Banner>
        : <p className="faint" role="status">{message.text}</p>)}

      <Card title="Downloaded">
        {models.data.length === 0 ? (
          <Empty>No models yet. Pick one below.</Empty>
        ) : (
          <table className="table">
            <thead><tr><th>Model</th><th>Source</th><th>Size</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {models.data.map((model) => (
                <Fragment key={model.id}>
                  <tr>
                    <td data-label="Model" className="mono">{model.name}</td>
                    <td data-label="Source">
                      <div>
                        <span className="mono">{model.repo_id}</span>
                        <div className="faint mono" style={{ fontSize: "0.75rem" }}>{(model.revision ?? "").slice(0, 12)}</div>
                      </div>
                    </td>
                    <td data-label="Size" style={{ whiteSpace: "nowrap" }}>{formatDecimalBytes(model.total_bytes)}</td>
                    <td data-label="Status" style={{ minWidth: "12rem" }}>
                      <div>
                        {ACTIVE_MODEL.includes(model.status) ? <Progress model={model} />
                          : <StatusPill status={model.status as BuildStatus} />}
                        {model.error && <div className="faint" style={{ fontSize: "0.8rem", color: "var(--fail)" }}>{model.error}</div>}
                      </div>
                    </td>
                    <td>
                      <div className="row" style={{ gap: "0.4rem", flexWrap: "wrap", justifyContent: "flex-end" }}>
                        {model.status === "ready" && (
                          <button onClick={() => setOpen(open === model.id ? null : model.id)}>
                            {open === model.id ? "Hide files" : "Files"}
                          </button>
                        )}
                        {model.status === "failed" && (
                          <button disabled={busy !== null} onClick={() => act(model.id, () => api.retryModel(model.id))}>
                            Retry
                          </button>
                        )}
                        <button className="danger" disabled={busy !== null} onClick={() => remove(model)}>
                          {ACTIVE_MODEL.includes(model.status) ? "Cancel" : "Delete"}
                        </button>
                      </div>
                    </td>
                  </tr>
                  {open === model.id && (
                    <tr>
                      <td colSpan={5}><ModelFiles model={model} /></td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <div style={{ marginTop: "1rem" }}>
        <Card title={`Recommended for ${data.gpu}`}>
          <p className="faint" style={{ marginTop: 0 }}>
            All open (no Hugging Face login). Sizes are what is downloaded: the files vLLM loads, not the whole
            repository. <em>GPUs</em> is the smallest number of H200 that serves the model at its stored precision.
          </p>
          {purposes.map(([purpose, label]) => {
            const entries = data.models.filter((entry) => entry.purpose === purpose);
            if (!entries.length) return null;
            return (
              <div key={purpose} style={{ marginTop: "0.8rem" }}>
                <h3>{label}</h3>
                <table className="table">
                  <tbody>
                    {entries.map((entry) => {
                      const tooBig = data.free_bytes !== null && entry.size_gb * 1e9 > data.free_bytes;
                      return (
                        <tr key={entry.key}>
                          <td data-label="Model">
                            <div>
                              <span className="mono" style={{ overflowWrap: "anywhere" }}>{entry.repo_id}</span>
                              <div className="faint" style={{ fontSize: "0.8rem" }}>{entry.why}</div>
                            </div>
                          </td>
                          <td data-label="Size">
                            <div>
                              <span>{entry.params} · {entry.precision}</span>
                              <div className="faint" style={{ whiteSpace: "nowrap" }}>
                                {entry.size_gb.toFixed(1)} GB · {entry.gpus} GPU{entry.gpus > 1 ? "s" : ""}
                              </div>
                            </div>
                          </td>
                          <td data-label="License" className="faint mono">{entry.license}</td>
                          <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                            {entry.local ? (
                              entry.local.status === "ready" ? <StatusPill status="ready" />
                                : ACTIVE_MODEL.includes(entry.local.status) ? <span className="faint">downloading…</span>
                                : <StatusPill status="failed" />
                            ) : (
                              <div>
                                <button className="primary" disabled={busy !== null || tooBig}
                                        onClick={() => fetchCatalog(entry)}>
                                  {busy === entry.key ? "Checking…" : "Download"}
                                </button>
                                {tooBig && <div className="faint" style={{ fontSize: "0.75rem" }}>needs more free space</div>}
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            );
          })}
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}>
        <Card title="Any Hugging Face model">
          <form className="row" style={{ gap: "0.5rem", flexWrap: "wrap", alignItems: "flex-end" }} onSubmit={fetchCustom}>
            <label style={{ flex: "2 1 18rem" }}>
              <span className="faint" style={{ fontSize: "0.8rem" }}>Model id</span>
              <input value={repo} onChange={(event) => setRepo(event.target.value)} placeholder="Qwen/Qwen3-14B"
                     required pattern="[A-Za-z0-9][A-Za-z0-9._\-]*/[A-Za-z0-9][A-Za-z0-9._\-]*" />
            </label>
            <label style={{ flex: "1 1 8rem" }}>
              <span className="faint" style={{ fontSize: "0.8rem" }}>Revision</span>
              <input value={revision} onChange={(event) => setRevision(event.target.value)} />
            </label>
            <button className="primary" type="submit" disabled={busy !== null || !repo.trim()}>
              {busy === "custom" ? "Checking…" : "Download"}
            </button>
          </form>
          <p className="faint" style={{ marginBottom: 0 }}>
            Open models with <span className="mono">config.json</span> and <span className="mono">.safetensors</span>{" "}
            weights only. Gated models (Meta Llama, Google Gemma) are refused; RedHatAI publishes open copies of Llama.
          </p>
        </Card>
      </div>
    </>
  );
}
