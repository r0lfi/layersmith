import { useState } from "react";
import { api, BuildFile, DownloadFile, formatDecimalBytes } from "../api";
import { CodeBlock } from "./Training";
import { Banner, Card, useLoad } from "./ui";

/** The full address of a download, as wget on another machine needs it. */
export function absoluteUrl(path: string): string {
  return new URL(path, window.location.href).href;
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/**
 * One wget command for a file. `-c` resumes an interrupted download (the
 * server answers Range requests). `-O` only when the URL does not already end
 * in the file name; without a known name, the server's Content-Disposition
 * name is used.
 */
export function wgetCommand(url: string, name?: string): string {
  const full = absoluteUrl(url);
  if (!name) return `wget --content-disposition ${shellQuote(full)}`;
  const parsed = new URL(full);
  const last = decodeURIComponent(parsed.pathname.split("/").pop() ?? "");
  return parsed.search || last !== name
    ? `wget -c -O ${shellQuote(name)} ${shellQuote(full)}`
    : `wget -c ${shellQuote(full)}`;
}

/** Clipboard API needs HTTPS or localhost; fall back to a selected textarea elsewhere. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through */
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let done = false;
  try {
    done = document.execCommand("copy");
  } catch {
    done = false;
  }
  area.remove();
  return done;
}

function CopyButton({ text, label, title }: { text: string; label: string; title: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <button type="button" className="ghost copy-link" title={title}
            onClick={async () => {
              const ok = await copyText(text);
              setState(ok ? "copied" : "failed");
              if (!ok) window.prompt("Copy:", text);
              setTimeout(() => setState("idle"), 1500);
            }}>
      {state === "copied" ? "Copied" : label}
    </button>
  );
}

/** "URL" and "wget" buttons next to a download link. */
export function CopyLink({ url, name }: { url: string; name?: string }) {
  return (
    <span className="copy-links">
      <CopyButton text={absoluteUrl(url)} label="URL" title={`Copy the download URL: ${absoluteUrl(url)}`} />
      <CopyButton text={wgetCommand(url, name)} label="wget" title={`Copy: ${wgetCommand(url, name)}`} />
    </span>
  );
}

/** wget commands for every file of a list, with download-all.sh first when there is one. */
export function WgetList({ files, script, open = false }: {
  files: Pick<DownloadFile, "name" | "url">[];
  /** A generated download-all.sh: fetches, resumes and checks every file with curl. */
  script?: { name: string; url: string };
  open?: boolean;
}) {
  if (!files.length) return null;
  const lines: string[] = [];
  if (script) {
    lines.push("# One script: fetches every file, resumes, checks sha256", `${wgetCommand(script.url, script.name)} && bash ${shellQuote(script.name)}`, "", "# or file by file:");
  }
  lines.push(...files.map((file) => wgetCommand(file.url, file.name)));
  return (
    <details className="wget-list" open={open}>
      <summary>Download with wget or curl ({files.length} URL{files.length === 1 ? "" : "s"})</summary>
      <p className="faint" style={{ margin: "0.3rem 0" }}>
        Plain HTTP GET links: run these on any machine that reaches this LayerSmith server.{" "}
        <span className="mono">-c</span> continues an interrupted download. If LayerSmith sits behind a
        login proxy, that machine must be let through it.
      </p>
      <CodeBlock code={lines.join("\n")} />
    </details>
  );
}

/** Every file of a build, grouped, each with its URL. */
export function BuildFilesTable({ files }: { files: BuildFile[] }) {
  const groups = [...new Set(files.map((file) => file.group))];
  return (
    <>
      {groups.map((group) => (
        <div key={group} style={{ marginBottom: "0.8rem" }}>
          <h3>{group}</h3>
          <table className="table">
            <thead><tr><th>File</th><th>Size</th><th>URL</th><th></th></tr></thead>
            <tbody>
              {files.filter((file) => file.group === group).map((file) => (
                <tr key={file.url}>
                  <td data-label="File" className="mono">{file.name}</td>
                  <td data-label="Size" style={{ whiteSpace: "nowrap" }}>
                    {file.size !== undefined ? formatDecimalBytes(file.size) : "—"}
                  </td>
                  <td data-label="URL" className="mono faint" style={{ overflowWrap: "anywhere" }}>{absoluteUrl(file.url)}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <a className="button" href={file.url}>Download</a> <CopyLink url={file.url} name={file.name} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}

/** "Download links": every file of the build with its URL, for wget on another machine. */
export function BuildDownloadLinks({ buildId, version }: {
  buildId: string;
  /** Changes whenever a file may have appeared or gone (split, bundle, export), to reload the list. */
  version: string;
}) {
  const { data, error, reload } = useLoad(() => api.buildFiles(buildId), [buildId, version]);
  return (
    <div style={{ marginTop: "1rem" }}>
      <Card title="Download links" actions={<button className="ghost" onClick={reload}>Refresh</button>}>
        <p className="faint" style={{ marginTop: 0 }}>
          Every file of this build that can be downloaded now, with a direct URL. Copy one with{" "}
          <span className="mono">URL</span> or a ready command with <span className="mono">wget</span>.
        </p>
        {error && <Banner>{error}</Banner>}
        {!data ? <p className="dim">Loading…</p> : data.files.length === 0 ? (
          <p className="dim">No files yet: the image archive appears here once the build is ready.</p>
        ) : (
          <>
            <WgetList files={data.files} />
            <BuildFilesTable files={data.files} />
          </>
        )}
      </Card>
    </div>
  );
}
