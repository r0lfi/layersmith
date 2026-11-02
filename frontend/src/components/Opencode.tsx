import { api, Build, formatBytes } from "../api";
import { Banner, Card } from "./ui";
import { CodeBlock } from "./Training";
import { DownloadAllButton } from "./DownloadAll";
import { CheckRows } from "./OpenShift";

/** Everything a finished opencode Shared Terminal build shows on its page. */
export function OpencodePanel({ build }: { build: Build }) {
  const detail = build.opencode!;
  if (detail.error) return <Card title="opencode Shared Terminal"><Banner>{detail.error}</Banner></Card>;
  const recipe = detail.recipe;
  const c = detail.commands;
  const ready = build.status === "ready";
  const problems = detail.tools_installed.filter((tool) => tool.status !== "ok");

  return (
    <>
      <div className="grid two">
        <Card title={`${recipe.profile_name}: opencode ${recipe.opencode.version}`}>
          <dl className="kv">
            <dt>Base</dt><dd>{recipe.base.display}{!recipe.base.verified && <span className="warn-text"> · custom base</span>}</dd>
            <dt>Base digest</dt><dd className="mono truncate-long" title={build.base_digest ?? ""}>{build.base_digest ?? "—"}</dd>
            <dt>opencode</dt><dd>{recipe.opencode.version} · <span className="mono">{recipe.opencode.asset}</span></dd>
            <dt>Toolchains</dt><dd>{recipe.optional.length ? recipe.optional.join(", ") : "none"}</dd>
            <dt>Image size</dt><dd>{formatBytes(build.image_size)}</dd>
            <dt>Archive</dt><dd>{detail.archive_format}</dd>
          </dl>
        </Card>

        <Card title="Checks">
          <CheckRows rows={detail.tests} />
          <p className="faint">
            Both run inside the image with networking disabled and every capability dropped, as the shared container
            runs. A failed check does not fail the build; it is shown here and recorded in the air-gap bundle.
          </p>
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}><Card title="Installed tools and versions">
        {detail.tools_installed.length === 0 ? (
          <p className="faint">{build.status === "testing" ? "The offline check is running…" : "Not recorded: the offline check did not run for this build."}</p>
        ) : (
          <>
            {problems.length > 0 && (
              <Banner kind="warn">{problems.map((tool) => `${tool.binary}: ${tool.status}`).join(", ")}</Banner>
            )}
            <table className="table">
              <thead><tr><th>Tool</th><th>Binary</th><th>Version</th><th>Status</th><th>Path</th></tr></thead>
              <tbody>
                {detail.tools_installed.map((tool) => (
                  <tr key={tool.binary}>
                    <td data-label="Tool">{tool.tool}</td>
                    <td data-label="Binary" className="mono">{tool.binary}</td>
                    <td data-label="Version" className="mono">{tool.version}</td>
                    <td data-label="Status" className={tool.status === "ok" ? "" : "warn-text"}>{tool.status}</td>
                    <td data-label="Path" className="mono faint">{tool.path}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </Card></div>

      <div className="grid two" style={{ marginTop: "1rem" }}>
        <Card title="Install on the host (once, as root)">
          {ready && detail.downloads.length > 0 && (
            <div style={{ marginBottom: "0.8rem" }}>
              <DownloadAllButton files={detail.downloads} nextStep={c.install} host="air-gapped host" />
            </div>
          )}
          <CodeBlock code={`${c.verify}\n${c.install}`} />
          <p className="faint">
            <a href={api.opencodeFileUrl(build.id, "install-host.sh")}>install-host.sh</a> loads the image, takes the host
            files out of the image itself, and sets up the <span className="mono">opencode</span> command, a sudo rule for
            the <span className="mono">opencode</span> group and the <span className="mono">opencode-container</span> service
            that keeps the container running. Options: <span className="mono">--mounts "/home /srv"</span>,{" "}
            <span className="mono">--users "alice bob"</span>, <span className="mono">--runtime docker</span>.{" "}
            <a href={api.opencodeFileUrl(build.id, "README.md")}>README.md</a>
          </p>
          <h4>Give people access, then they just type opencode</h4>
          <CodeBlock code={`${c.add_user}\n\n# as alice:\n${c.use}`} />
          <p className="faint">
            Everyone runs as themselves, with their own groups, in the directory they are in; sessions and settings stay
            in their own home. <span className="mono">{c.shell}</span> opens a shell in the same place.
          </p>
        </Card>

        <Card title="Point it at your LLM">
          <p className="faint" style={{ marginTop: 0 }}>
            Edit <span className="mono">/etc/opencode/opencode.json</span> on the host (written once by the installer, never
            overwritten). It applies to the next <span className="mono">opencode</span> started. Any OpenAI-compatible
            endpoint works - vLLM, Ollama, LiteLLM - the provider is built into opencode, nothing comes from npm.
          </p>
          <CodeBlock code={detail.site_config} />
          <h4>Operating it</h4>
          <CodeBlock code={`${c.status}\n${c.restart}\n${c.update}   # a new version\n${c.uninstall}`} />
        </Card>
      </div>
    </>
  );
}
