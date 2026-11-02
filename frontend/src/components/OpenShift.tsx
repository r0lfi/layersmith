import { api, Build, formatBytes, OpenShiftTestRow } from "../api";
import { Banner, Card } from "./ui";
import { CodeBlock } from "./Training";
import { DownloadAllButton } from "./DownloadAll";

const STATUS_ICON: Record<string, string> = { passed: "✓", failed: "✕", not_run: "–" };

export function CheckRows({ rows }: { rows: OpenShiftTestRow[] }) {
  return (
    <div className="checks">
      {rows.map((row) => (
        <details key={row.id} className={`check-row ${row.status}`}>
          <summary>
            <span className="check-icon" aria-hidden="true">{STATUS_ICON[row.status]}</span>
            <span className="check-label">{row.label} <span className="faint">· as {row.runs_as}</span></span>
            <span className="check-summary faint">{row.summary}</span>
          </summary>
          <div className="check-body">
            <p className="faint">{row.what}</p>
            {row.steps.length > 0 && (
              <table className="table">
                <tbody>
                  {row.steps.map((step, index) => (
                    <tr key={index}>
                      <td data-label="Step">{step.name}</td>
                      <td data-label="Result" className={step.status === "failed" ? "warn-text" : ""}>{step.status}</td>
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

/** Everything a finished OpenShift Admin Toolkit build shows on its page. */
export function OpenShiftPanel({ build }: { build: Build }) {
  const detail = build.openshift!;
  if (detail.error) return <Card title="OpenShift Admin Toolkit"><Banner>{detail.error}</Banner></Card>;
  const recipe = detail.recipe;
  const c = detail.commands;
  const ready = build.status === "ready";
  const problems = detail.tools_installed.filter((tool) => tool.status !== "ok");

  return (
    <>
      <div className="grid two">
        <Card title={`${recipe.profile_name}: OpenShift ${recipe.ocp.version}`}>
          <dl className="kv">
            <dt>Base</dt><dd>{recipe.base.display}{!recipe.base.verified && <span className="warn-text"> · custom base</span>}</dd>
            <dt>Base digest</dt><dd className="mono truncate-long" title={build.base_digest ?? ""}>{build.base_digest ?? "—"}</dd>
            <dt>Clients</dt><dd>oc {recipe.ocp.version} · kubectl {recipe.ocp.kubernetes} ({recipe.ocp.channel})</dd>
            <dt>Clusters</dt><dd>OpenShift {recipe.compatibility.supported_clusters.join(", ")}</dd>
            <dt>Time zone</dt><dd>{recipe.timezone} · {recipe.locale}</dd>
            <dt>Production</dt><dd>contexts matching <span className="mono">{recipe.prod_pattern}</span></dd>
            <dt>Optional</dt><dd>{recipe.optional.length ? recipe.optional.join(", ") : "none"}</dd>
            <dt>Image size</dt><dd>{formatBytes(build.image_size)}</dd>
            <dt>Archive</dt><dd>{detail.archive_format}</dd>
          </dl>
          <p className="faint">{recipe.compatibility.note}</p>
        </Card>

        <Card title="Checks">
          <CheckRows rows={detail.tests} />
          <p className="faint">
            Both run inside the image with networking disabled and every capability dropped. A failed check does not
            fail the build; it is shown here and recorded in the air-gap bundle.
          </p>
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}><Card title="Installed tools and versions">
        {detail.tools_installed.length === 0 ? (
          <p className="faint">{build.status === "testing" ? "The user check is running…" : "Not recorded: the user check did not run for this build."}</p>
        ) : (
          <>
            {problems.length > 0 && (
              <Banner kind="warn">
                {problems.map((tool) => `${tool.binary}: ${tool.status}`).join(", ")} — a missing or unexpected tool is
                reported, never counted as installed.
              </Banner>
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
            {ready && <p className="faint"><a href={api.openshiftFileUrl(build.id, "versions.json")}>versions.json</a> · <a href={api.openshiftFileUrl(build.id, "checks.json")}>checks.json</a></p>}
          </>
        )}
      </Card></div>

      <div style={{ marginTop: "1rem" }}><Card title="Always running: the openshift-admin command">
        {ready && detail.downloads.length > 0 && (
          <div style={{ marginBottom: "0.8rem" }}>
            <DownloadAllButton files={detail.downloads} nextStep={c.install} host="workstation" />
          </div>
        )}
        <p className="faint" style={{ marginTop: 0 }}>
          One command, as your own user (no sudo): loads the image, installs <span className="mono">openshift-admin</span>{" "}
          and a systemd user service that keeps the container running. A split download does the same
          with <span className="mono">{c.assemble_install}</span>.
        </p>
        <CodeBlock code={`${c.install}\n\n${c.enter}                     # a shell: oc login, oc get nodes, k9s ...\n${c.run_one}`} />
        <p className="faint">
          A new version: run the same with the new archive; the container is recreated on it and{" "}
          <span className="mono">~/.kube</span> and its home are kept.{" "}
          <a href={api.openshiftFileUrl(build.id, "install-host.sh")}>install-host.sh</a>
        </p>
      </Card></div>

      <div className="grid two" style={{ marginTop: "1rem" }}>
        <Card title="Or with Distrobox">
          <CodeBlock code={`${c.verify}\n${c.load}\n${c.tag}\n\n${c.distrobox_create}\n${c.distrobox_enter}`} />
          <p className="faint">
            Or in one step with the Assemble manifest (<a href={api.openshiftFileUrl(build.id, "openshift-admin.ini")}>openshift-admin.ini</a>):
          </p>
          <CodeBlock code={`${c.distrobox_assemble}\n${c.distrobox_enter}`} />
          <p className="faint">
            Inside: <span className="mono">oc login https://api.&lt;cluster&gt;:6443</span>, then <span className="mono">oc-status</span>,{" "}
            <span className="mono">oc-gpu</span>, <span className="mono">k9s</span>. Your <span className="mono">~/.kube</span> is
            used as it is; nothing about a cluster is stored in the image. <a href={api.openshiftFileUrl(build.id, "README.md")}>README.md</a>
          </p>
        </Card>

        <Card title="Without Distrobox, OCI archive">
          <p className="faint" style={{ marginTop: 0 }}>
            Distrobox shares your home, the host's network and mounts the host's root at <span className="mono">/run/host</span>;
            it also starts containers rootless-privileged. For a container that sees only <span className="mono">~/.kube</span>,
            with its own network:
          </p>
          <CodeBlock code={c.podman_run} />
          <p className="faint">Import the OCI archive without podman load, inspect it, or export the image again:</p>
          <CodeBlock code={`${c.skopeo_import}\n${c.inspect_archive}\n${c.oci_export}`} />
          <p className="faint">
            podman, buildah and skopeo are installed; building images inside Distrobox needs nested user namespaces.
            <span className="mono"> host-podman</span> runs the host's Podman explicitly instead; the socket is never mounted.
          </p>
        </Card>
      </div>
    </>
  );
}
