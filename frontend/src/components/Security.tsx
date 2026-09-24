import { useCallback, useEffect, useMemo, useState } from "react";
import { Finding, SCAN_ACTIVE, SEVERITIES, Scan, Severity, api, formatWhen } from "../api";
import { Banner, Card, Empty, usePoll } from "./ui";

/** Severity counts as a row of chips. Zero counts stay visible but quiet. */
export function SeverityBar({ counts, onPick, active }: {
  counts: Record<string, number>;
  onPick?: (severity: Severity | null) => void;
  active?: Severity | null;
}) {
  return (
    <div className="sev-bar">
      {SEVERITIES.map((severity) => {
        const count = counts?.[severity] ?? 0;
        const classes = ["sev", severity, count ? "" : "zero", active === severity ? "picked" : ""];
        return onPick ? (
          <button
            key={severity}
            className={classes.join(" ")}
            onClick={() => onPick(active === severity ? null : severity)}
            title={`${count} ${severity}`}
          >
            <span className="sev-count">{count}</span>
            <span className="sev-name">{severity}</span>
          </button>
        ) : (
          <span key={severity} className={classes.join(" ")} title={`${count} ${severity}`}>
            <span className="sev-count">{count}</span>
            <span className="sev-name">{severity}</span>
          </span>
        );
      })}
    </div>
  );
}

function ScanStatus({ scan }: { scan: Scan }) {
  if (scan.state === "exporting") {
    return <span className="pill running"><span className="dot" />exporting the image</span>;
  }
  if (scan.state === "scanning") {
    return <span className="pill running"><span className="dot" />scanning</span>;
  }
  if (scan.state === "queued") return <span className="pill queued"><span className="dot" />queued</span>;
  if (scan.state === "failed") return <span className="pill failed"><span className="dot" />failed</span>;
  return <span className="pill ready"><span className="dot" />scanned</span>;
}

function FindingRow({ finding }: { finding: Finding }) {
  const identifier = finding.url ? (
    <a href={finding.url} target="_blank" rel="noreferrer noopener">{finding.identifier}</a>
  ) : (
    finding.identifier
  );
  return (
    <tr>
      <td data-label="Severity"><span className={`sev-tag ${finding.severity}`}>{finding.severity}</span></td>
      <td data-label="Finding" className="mono">{identifier}</td>
      <td data-label="Package">
        {finding.kind === "vulnerability" ? (
          <>
            <span className="mono">{finding.package_name}</span>{" "}
            <span className="faint mono">{finding.installed_version}</span>
          </>
        ) : (
          <span className="mono truncate" title={finding.target}>{finding.target}</span>
        )}
      </td>
      <td data-label="Fixed in">
        {finding.kind === "secret" ? (
          // Never the value: a secret finding carries a location and a length.
          <span className="faint">{finding.masked_match ?? "value not stored"}</span>
        ) : finding.fixed_version ? (
          <span className="mono fixed">{finding.fixed_version}</span>
        ) : (
          <span className="faint">no fix yet</span>
        )}
      </td>
      <td data-label="Title" className="faint truncate" title={finding.title}>{finding.title}</td>
    </tr>
  );
}

const KINDS = [
  { key: "vulnerability", label: "Vulnerabilities" },
  { key: "secret", label: "Secrets" },
  { key: "misconfiguration", label: "Misconfigurations" },
];

/** The security panel on a build: the latest scan, its findings, and history. */
export function SecurityPanel({ buildId, scannerAvailable, scannerDetail }: {
  buildId: string;
  scannerAvailable: boolean;
  scannerDetail: string;
}) {
  const [scans, setScans] = useState<Scan[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Scan | null>(null);
  const [kind, setKind] = useState<string>("vulnerability");
  const [severity, setSeverity] = useState<Severity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadScans = useCallback(() => {
    api.scans(buildId)
      .then((rows) => {
        setScans(rows);
        setSelectedId((current) => current ?? rows[0]?.id ?? null);
      })
      .catch((exc: Error) => setError(exc.message));
  }, [buildId]);

  useEffect(loadScans, [loadScans]);

  const selected = useMemo(
    () => scans?.find((scan) => scan.id === selectedId) ?? scans?.[0] ?? null,
    [scans, selectedId],
  );
  const running = !!selected && SCAN_ACTIVE.includes(selected.state);

  // While a scan is exporting or scanning, keep the state moving on screen.
  usePoll(loadScans, running, 2000);

  useEffect(() => {
    if (!selected || selected.state !== "completed") {
      setDetail(null);
      return;
    }
    api.scan(selected.id, { kind, severity: severity ?? undefined })
      .then(setDetail)
      .catch((exc: Error) => setError(exc.message));
  }, [selected?.id, selected?.state, kind, severity]);

  async function rescan() {
    setBusy(true);
    setError(null);
    try {
      const scan = await api.startScan(buildId);
      setSelectedId(scan.id);
      loadScans();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const title = (
    <div className="row">
      <h2>Security</h2>
      {selected && <ScanStatus scan={selected} />}
    </div>
  );

  return (
    <Card
      title={title}
      actions={
        scannerAvailable && (
          <button onClick={rescan} disabled={busy || running}>
            {busy || running ? "Scanning…" : scans?.length ? "Rescan" : "Scan image"}
          </button>
        )
      }
    >
      {error && <Banner>{error}</Banner>}

      {!scannerAvailable && (
        <Banner>
          This image has not been scanned, so nothing is known about its vulnerabilities — which is not
          the same as it being clean. {scannerDetail}
        </Banner>
      )}

      {scannerAvailable && scans?.length === 0 && (
        <Empty>This image has not been scanned yet.</Empty>
      )}

      {selected?.state === "failed" && <Banner>The scan did not finish: {selected.error}</Banner>}

      {selected && selected.state !== "failed" && (
        <>
          <SeverityBar
            counts={selected.counts?.[kind] ?? {}}
            onPick={selected.state === "completed" ? setSeverity : undefined}
            active={severity}
          />
          <p className="faint" style={{ marginTop: "0.5rem" }}>
            {selected.state === "completed" ? (
              <>
                Scanned {formatWhen(selected.finished_at)} with{" "}
                <span className="mono">{selected.scanner} {selected.scanner_version}</span>
                {selected.database?.updated_at && (
                  <> using data from {formatWhen(selected.database.updated_at)}</>
                )}
                {selected.database?.offline && " (imported offline)"}. A scan reflects what that database
                knew at that moment, not what is true today.
              </>
            ) : selected.state === "exporting" ? (
              "Exporting the image so it can be scanned without giving the scanner access to the container runtime. Large images take a while."
            ) : (
              "Scanning…"
            )}
          </p>
        </>
      )}

      {selected?.state === "completed" && (
        <>
          <div className="tabs" style={{ marginTop: "0.8rem" }}>
            {KINDS.filter((row) => selected.kinds.includes(row.key) || selected.counts?.[row.key]).map((row) => (
              <button
                key={row.key}
                className={kind === row.key ? "tab active" : "tab"}
                onClick={() => { setKind(row.key); setSeverity(null); }}
              >
                {row.label}
                <span className="tab-count">
                  {Object.values(selected.counts?.[row.key] ?? {}).reduce((sum, value) => sum + value, 0)}
                </span>
              </button>
            ))}
          </div>

          {detail?.findings?.length ? (
            <table className="table findings">
              <thead>
                <tr>
                  <th>Severity</th><th>Finding</th>
                  <th>{kind === "vulnerability" ? "Package" : "Location"}</th>
                  <th>{kind === "secret" ? "Match" : "Fixed in"}</th>
                  <th>Title</th>
                </tr>
              </thead>
              <tbody>
                {detail.findings.map((finding) => <FindingRow key={finding.id} finding={finding} />)}
              </tbody>
            </table>
          ) : (
            <Empty>
              {severity
                ? `No ${severity} findings of this kind.`
                : "Nothing found of this kind — which is not a guarantee, only the absence of a match."}
            </Empty>
          )}

          <div className="row" style={{ marginTop: "0.8rem", gap: "0.8rem", flexWrap: "wrap" }}>
            {selected.sbom && (
              <a className="button" href={api.sbomUrl(selected.id)}>
                Download SBOM ({selected.sbom.format}, {selected.sbom.components} components)
              </a>
            )}
          </div>
        </>
      )}

      {scans && scans.length > 1 && (
        <>
          <h3 style={{ marginTop: "1.2rem" }}>Scan history</h3>
          <p className="faint" style={{ marginTop: "0.2rem" }}>
            Every scan of this exact image is kept, so what was known when stays answerable.
          </p>
          <table className="table">
            <thead>
              <tr><th>When</th><th>Reason</th><th>State</th><th>Findings</th><th /></tr>
            </thead>
            <tbody>
              {scans.map((scan) => (
                <tr key={scan.id} className={scan.id === selected?.id ? "picked-row" : undefined}>
                  <td data-label="When" className="dim">{formatWhen(scan.created_at)}</td>
                  <td data-label="Reason">{scan.reason === "build" ? "after build" : "on request"}</td>
                  <td data-label="State"><ScanStatus scan={scan} /></td>
                  <td data-label="Findings">{scan.state === "completed" ? scan.total : "-"}</td>
                  <td data-label="">
                    {scan.id !== selected?.id && (
                      <button className="link" onClick={() => setSelectedId(scan.id)}>Show</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Card>
  );
}
