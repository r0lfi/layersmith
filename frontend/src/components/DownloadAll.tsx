import { useRef, useState } from "react";
import { DownloadFile, formatDecimalBytes } from "../api";

/* The File System Access API (Chrome, Edge) is not in TypeScript's DOM types yet. */
type DirectoryHandle = {
  name: string;
  getFileHandle(name: string, options?: { create?: boolean }): Promise<{
    getFile(): Promise<File>;
    createWritable(): Promise<WritableStream<Uint8Array>>;
  }>;
};
type PickerWindow = Window & { showDirectoryPicker?: (options?: { mode?: string }) => Promise<DirectoryHandle> };

interface Progress {
  index: number;
  name: string;
  fileDone: number;
  fileSize: number;
  done: number;
  total: number;
  skipped: number;
}

/**
 * Download every file of a kit from the browser.
 *
 * Where the browser can write to a folder (Chrome, Edge), each file is
 * streamed straight to disk - nothing is held in memory, files already there
 * with the right size are skipped, so a stopped run resumes. Elsewhere the
 * files are handed to the browser's own download list one after another.
 * Checksums are verified on the GPU host by setup.sh and the install scripts.
 */
export function DownloadAllButton({ files, label = "Download all", nextStep = "bash setup.sh" }: {
  files: DownloadFile[];
  label?: string;
  /** What to run on the GPU host once the folder is copied there. */
  nextStep?: string;
}) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [message, setMessage] = useState<{ error: boolean; text: string } | null>(null);
  const abort = useRef<AbortController | null>(null);
  const picker = (window as PickerWindow).showDirectoryPicker;
  const total = files.reduce((sum, file) => sum + (file.size ?? 0), 0);

  async function toFolder() {
    let directory: DirectoryHandle;
    try {
      directory = await picker!.call(window, { mode: "readwrite" });
    } catch {
      return; // the picker was cancelled
    }
    const controller = new AbortController();
    abort.current = controller;
    setMessage(null);
    let done = 0;
    let skipped = 0;
    try {
      for (const [index, file] of files.entries()) {
        const handle = await directory.getFileHandle(file.name, { create: true });
        const existing = await handle.getFile();
        if (file.size !== undefined && existing.size === file.size) {
          done += file.size;
          skipped += 1;
          setProgress({ index, name: file.name, fileDone: file.size, fileSize: file.size, done, total, skipped });
          continue;
        }
        const response = await fetch(file.url, { signal: controller.signal });
        if (!response.ok || !response.body) throw new Error(`${file.name}: ${response.status} ${response.statusText}`);
        const size = file.size ?? Number(response.headers.get("content-length") ?? 0);
        let fileDone = 0;
        let shown = 0;
        const counter = new TransformStream<Uint8Array, Uint8Array>({
          transform(chunk, sink) {
            fileDone += chunk.byteLength;
            // A few updates a second; one per chunk would re-render thousands of times per part.
            if (Date.now() - shown > 250) {
              shown = Date.now();
              setProgress({ index, name: file.name, fileDone, fileSize: size, done: done + fileDone, total, skipped });
            }
            sink.enqueue(chunk);
          },
        });
        await response.body.pipeThrough(counter).pipeTo(await handle.createWritable(), { signal: controller.signal });
        done += fileDone;
      }
      const where = directory.name ? `the folder “${directory.name}”` : "the chosen folder";
      setMessage({ error: false, text: `All ${files.length} files are in ${where}` +
        (skipped ? ` (${skipped} were already there).` : ".") +
        ` Copy the folder to the GPU host and run: ${nextStep}` });
    } catch (exc) {
      const stopped = controller.signal.aborted;
      setMessage({ error: !stopped, text: stopped
        ? "Stopped. Press the button again and choose the same folder to continue where it stopped."
        : `${(exc as Error).message}. Press the button again and choose the same folder to continue.` });
    } finally {
      abort.current = null;
      setProgress(null);
    }
  }

  async function toDownloads() {
    setMessage({ error: false, text: `Handing ${files.length} files to the browser's downloads, one by one. ` +
      "If the browser asks whether this page may download multiple files, allow it." });
    for (const file of files) {
      const link = document.createElement("a");
      link.href = file.url;
      link.download = file.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }

  const running = progress !== null;
  const percent = progress && progress.total ? Math.floor((progress.done / progress.total) * 100) : 0;
  return (
    <div style={{ flexBasis: "100%" }}>
      <div className="row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
        {running ? (
          <button className="danger" onClick={() => abort.current?.abort()}>Stop</button>
        ) : (
          <button className="primary" disabled={!files.length} onClick={picker ? toFolder : toDownloads}
                  title={picker ? "Choose a folder; every file is saved into it" : "Every file goes to your downloads"}>
            {label} ({files.length} files, {formatDecimalBytes(total)})
          </button>
        )}
        {!running && (
          <span className="faint" style={{ fontSize: "0.85rem" }}>
            {picker ? "Choose an empty folder (or the same one again to resume)."
              : "Your browser saves each file to its download folder."}
          </span>
        )}
      </div>
      {progress && (
        <div style={{ marginTop: "0.5rem" }}>
          <div className="progress" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
            <span style={{ width: `${percent}%` }} />
          </div>
          <p className="faint" style={{ margin: "0.3rem 0 0", fontSize: "0.85rem" }}>
            {percent}% · {formatDecimalBytes(progress.done)} of {formatDecimalBytes(progress.total)} · file{" "}
            {progress.index + 1}/{files.length}: <span className="mono">{progress.name}</span>{" "}
            ({formatDecimalBytes(progress.fileDone)} of {formatDecimalBytes(progress.fileSize)})
          </p>
        </div>
      )}
      {message && (
        <p className={message.error ? "warning" : "faint"} role="status" style={{ marginBottom: 0 }}>{message.text}</p>
      )}
    </div>
  );
}
