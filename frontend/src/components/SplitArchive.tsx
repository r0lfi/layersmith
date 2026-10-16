import { useCallback, useEffect, useState } from "react";
import { api, formatDecimalBytes, SplitStatus } from "../api";
import { Banner, usePoll } from "./ui";

/** Status of a build's split download, polled while the server computes it. */
export function useSplit(buildId: string, enabled: boolean) {
  const [status, setStatus] = useState<SplitStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const refresh = useCallback(() => {
    api.split(buildId).then(setStatus).catch((exc) => setError((exc as Error).message));
  }, [buildId]);

  useEffect(() => {
    if (enabled) refresh();
  }, [enabled, refresh]);
  usePoll(refresh, status?.state === "running", 1000);

  async function start() {
    setStarting(true);
    setError(null);
    try {
      setStatus(await api.startSplit(buildId));
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setStarting(false);
    }
  }

  return { status, error, starting, start };
}

type Split = ReturnType<typeof useSplit>;

/** The button and plan, under "Download TAR" in the Image archive column. */
export function SplitControls({ split }: { split: Split }) {
  const { status, starting, start } = split;
  if (!status?.available) return null;
  const running = status.state === "running";
  const percent = status.progress?.total ? Math.floor((status.progress.done / status.progress.total) * 100) : 0;
  return (
    <div style={{ marginTop: "0.6rem" }}>
      {status.state === "ready" ? (
        <a className="button" href="#split-archive">Split archive ready ↓</a>
      ) : (
        <button onClick={start} disabled={starting || running}>
          {running ? `Splitting… ${percent}%` : starting ? "Starting…" : "Download split archive"}
        </button>
      )}
      <p className="faint" style={{ margin: "0.4rem 0 0", fontSize: "0.85rem" }}>
        Part size: {formatDecimalBytes(status.part_size)} · Total parts: {status.planned_parts}
        <br />
        For transfers that refuse files of 1 GB or more. Same bytes, no compression.
      </p>
    </div>
  );
}

/** Progress, then every part with checksums and assemble-image.sh, full width. */
export function SplitFiles({ buildId, split }: { buildId: string; split: Split }) {
  const { status, error } = split;
  if (error) return <Banner>{error}</Banner>;
  if (!status || status.state === "none") return null;

  if (status.state === "failed") {
    return <Banner>Splitting the archive failed: {status.error} — the TAR download is unaffected; try again.</Banner>;
  }

  if (status.state === "running") {
    const done = status.progress?.done ?? 0;
    const total = status.progress?.total ?? status.archive_size;
    const percent = total ? Math.floor((done / total) * 100) : 0;
    return (
      <div id="split-archive" style={{ marginTop: "1rem" }}>
        <h3>Split archive</h3>
        <p className="faint" style={{ margin: "0.2rem 0 0.5rem" }}>
          Computing a checksum for each part: {formatDecimalBytes(done)} of {formatDecimalBytes(total)} read.
        </p>
        <div className="progress" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
          <span style={{ width: `${percent}%` }} />
        </div>
      </div>
    );
  }

  const parts = status.files.filter((file) => file.kind === "part");
  const extras = status.files.filter((file) => file.kind !== "part");
  const archive = status.archive ?? "image.tar";
  return (
    <div id="split-archive" style={{ marginTop: "1rem" }}>
      <h3>Split archive · {parts.length} part{parts.length === 1 ? "" : "s"}</h3>
      <p className="faint" style={{ marginTop: "0.2rem" }}>
        Every file is under 1 GB. Download all of them into one directory, including{" "}
        <span className="mono">{status.script}</span>; nothing else is needed on the receiving host but bash
        and coreutils.
      </p>
      <table className="table">
        <thead>
          <tr><th>File</th><th>Size</th><th>SHA-256</th><th></th></tr>
        </thead>
        <tbody>
          {[...parts, ...extras].map((file) => (
            <tr key={file.name}>
              <td data-label="File" className="mono">{file.name}</td>
              <td data-label="Size" style={{ whiteSpace: "nowrap" }}>{formatDecimalBytes(file.size)}</td>
              <td data-label="SHA-256" className="mono faint split-sha" title={file.sha256 ?? ""}>
                {file.sha256 ? file.sha256 : file.kind === "script" ? "restores and verifies" : "checksum list"}
              </td>
              <td>
                <a className={`button${file.kind === "script" ? " primary" : ""}`}
                   href={api.splitFileUrl(buildId, file.name)}>Download</a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>On the receiving host</h3>
      <pre className="code">{`chmod +x ${status.script}
./${status.script}            # checks, reassembles and verifies ${archive}
./${status.script} --load     # ... and runs podman load -i ${archive}

# or by hand:
sha256sum -c ${archive.replace(/\.tar$/, "")}.parts.sha256
cat ${archive}.part-* > ${archive}
sha256sum -c ${archive}.sha256
podman load -i ${archive}`}</pre>
    </div>
  );
}
