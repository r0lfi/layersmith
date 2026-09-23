import { ReactNode, useEffect, useState } from "react";
import { ACTIVE, BuildStatus } from "../api";

export function Card({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-head">
          {typeof title === "string" ? <h2>{title}</h2> : title}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({ value, label }: { value: ReactNode; label: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export function StatusPill({ status }: { status: BuildStatus }) {
  const group = status === "ready" ? "ready"
    : status === "failed" || status === "cancelled" ? "failed"
    : status === "queued" ? "queued"
    : "running";
  return (
    <span className={`pill ${group}`}>
      <span className="dot" aria-hidden="true" />
      {status}
    </span>
  );
}

export function isActive(status: BuildStatus): boolean {
  return ACTIVE.includes(status);
}

export function Banner({ kind = "error", children }: { kind?: "error" | "warn" | "info"; children: ReactNode }) {
  return <div className={`banner ${kind}`}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Fetch-on-mount with loading and error state, and a manual reload. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loader()
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      })
      .catch((exc: Error) => !cancelled && setError(exc.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { data, error, loading, reload: () => setTick((value) => value + 1), setData };
}

/** Poll while `active` is true; used for lists that contain running builds. */
export function usePoll(callback: () => void, active: boolean, intervalMs = 3000) {
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(callback, intervalMs);
    return () => clearInterval(timer);
  }, [callback, active, intervalMs]);
}

export function Field({
  label, hint, children,
}: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  );
}
