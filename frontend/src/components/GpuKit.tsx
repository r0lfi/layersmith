import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatDecimalBytes, GpuKit as Kit, LlmModel } from "../api";
import { Banner, Card, usePoll } from "./ui";
import { FileTable } from "../pages/Models";
import { DownloadAllButton } from "./DownloadAll";

/** Everything an offline GPU host needs, in parts under 1 GB, plus setup.sh. */
export function GpuKit({ buildId }: { buildId: string }) {
  const [models, setModels] = useState<LlmModel[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [kit, setKit] = useState<Kit | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showFiles, setShowFiles] = useState(false);
  const [preselected, setPreselected] = useState(false);

  useEffect(() => {
    api.llmModels().then((all) => setModels(all.filter((model) => model.status === "ready")))
      .catch((exc) => setError((exc as Error).message));
  }, []);

  function load() {
    api.gpuKit(buildId, chosen).then((value) => {
      setKit(value);
      // Models picked in the wizard are ticked once, as soon as they are downloaded.
      if (!preselected && value.suggested.length) {
        setPreselected(true);
        setChosen((current) => Array.from(new Set([...current, ...value.suggested])));
      }
    }).catch((exc) => setError((exc as Error).message));
  }
  useEffect(load, [buildId, chosen.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  usePoll(load, kit?.image_split === "running" || Boolean(kit?.pending.some((p) => p.status !== "failed")), 3000);

  async function prepareImage() {
    setError(null);
    try {
      await api.startSplit(buildId);
      load();
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  const toggle = (id: string) =>
    setChosen((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  const script = (name: string) => kit?.files.find((file) => file.name === name);
  const progress = kit?.image_split_progress;

  return (
    <div style={{ marginTop: "1rem" }} id="gpu-kit">
      <Card title="GPU kit — image, models and setup.sh">
        <p className="faint" style={{ marginTop: 0 }}>
          For a GPU host without network access and a transfer limit under 1 GB: the image and the chosen models
          in parts of at most 900 MB, plus <span className="mono">setup.sh</span>, which checks disk space, restores
          and loads the image, installs the models in <span className="mono">/opt/models</span>, checks the NVIDIA
          Container Toolkit and starts the dashboard.
        </p>
        {error && <Banner>{error}</Banner>}

        <h3>1. Models</h3>
        {models.length === 0 ? (
          <p className="faint">
            No downloaded models yet — <Link to="/models">download some on the Models page</Link>. The kit works
            without models too (GPU compute, memory and NCCL tests).
          </p>
        ) : (
          <div className="row" style={{ gap: "0.4rem 1rem", flexWrap: "wrap" }}>
            {models.map((model) => (
              <label key={model.id} className="check">
                <input type="checkbox" checked={chosen.includes(model.id)} onChange={() => toggle(model.id)} />
                <span><span className="mono">{model.name}</span> <span className="faint">{formatDecimalBytes(model.total_bytes)}</span></span>
              </label>
            ))}
          </div>
        )}

        {kit && kit.pending.length > 0 && (
          <p className="faint">
            Chosen for this image, not ready yet:{" "}
            {kit.pending.map((item) => `${item.name} (${item.status})`).join(", ")}. They are ticked here when their
            download finishes — see the <Link to="/models">Models page</Link>.
          </p>
        )}

        <h3>2. Image parts</h3>
        {!kit ? <p className="dim">Loading…</p> : kit.image_split === "ready" ? (
          <p className="faint">Ready.</p>
        ) : kit.image_split === "running" ? (
          <div>
            <div className="progress"><span style={{ width: `${progress?.total ? (progress.done / progress.total) * 100 : 0}%` }} /></div>
            <p className="faint">Computing part checksums…</p>
          </div>
        ) : (
          <button onClick={prepareImage}>Prepare image parts</button>
        )}

        {kit && (
          <>
            {kit.problems.map((problem) => <Banner key={problem}>{problem}</Banner>)}
            <h3>3. Download</h3>
            <p className="faint" style={{ marginTop: 0 }}>
              {kit.files.length} files, {formatDecimalBytes(kit.total_bytes)} to carry over. The GPU host needs
              about {formatDecimalBytes(kit.gpu_host_needs_bytes)} free while installing (archive, unpacked image,
              models); <span className="mono">setup.sh</span> checks this per disk before it starts.
            </p>
            {kit.ready && (
              <div style={{ marginBottom: "0.8rem" }}>
                <DownloadAllButton files={kit.files.filter((file) => file.size !== undefined)} />
              </div>
            )}
            <div className="row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
              {kit.ready && script("download-all.sh") ? (
                <a className="button" href={script("download-all.sh")!.url}>download-all.sh</a>
              ) : (
                <button className="primary" disabled title="Prepare the image parts first">download-all.sh</button>
              )}
              {script("setup.sh") && <a className="button" href={script("setup.sh")!.url}>setup.sh</a>}
              {script("start_container.sh") && <a className="button" href={script("start_container.sh")!.url}>start_container.sh</a>}
              <button className="ghost" onClick={() => setShowFiles(!showFiles)}>
                {showFiles ? "Hide all files" : "All files"}
              </button>
            </div>
            {showFiles && <div style={{ marginTop: "0.6rem" }}><FileTable files={kit.files.filter((f) => f.size !== undefined)} /></div>}

            <h3>4. On each side</h3>
            <pre className="code">{`# in the browser: "Download all" into an empty folder, or with a script
# where LayerSmith is reachable (verifies every part, resumes):
mkdir gpu-kit && cd gpu-kit
bash ~/Downloads/download-all.sh

# copy the gpu-kit directory to the GPU host, then there:
bash setup.sh                 # check, load, install models, start the dashboard
bash setup.sh --no-start      # ... or only prepare
bash start_container.sh       # start again later`}</pre>
          </>
        )}
      </Card>
    </div>
  );
}
