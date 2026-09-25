// Small shared UI pieces: status badges (icon + label, never colour alone), time helpers, sparkline.
import type { ReactNode } from "react";

type Status = "good" | "warning" | "serious" | "critical" | "neutral";

const ICONS: Record<Status, ReactNode> = {
  good: <path d="M3 7.5 6 10.5 11 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />,
  warning: <path d="M7 2.2 12.4 11.6H1.6L7 2.2Z M7 6v2.4 M7 10v.1" fill="none" stroke="currentColor"
                 strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />,
  serious: <path d="M7 1.8v6.2 M7 10.8v.2 M2 7a5 5 0 1 0 10 0a5 5 0 1 0-10 0" fill="none" stroke="currentColor"
                 strokeWidth="1.5" strokeLinecap="round" />,
  critical: <path d="M4.6 1.5h4.8l3.1 3.1v4.8l-3.1 3.1H4.6L1.5 9.4V4.6Z M7 4.2v3.4 M7 9.8v.1" fill="none"
                  stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />,
  neutral: <circle cx="7" cy="7" r="2.5" fill="currentColor" />,
};

export function Badge({ status, children, title }: { status: Status; children: ReactNode; title?: string }) {
  return (
    <span className={`badge b-${status}`} title={title}>
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">{ICONS[status]}</svg>
      {children}
    </span>
  );
}

export const bandStatus = (band: string | null | undefined): Status =>
  band === "high" ? "critical" : band === "medium" ? "serious" : band === "low-medium" ? "warning"
    : band === "low" ? "good" : "neutral";

export const severityStatus = (s: string): Status =>
  s === "critical" ? "critical" : s === "high" ? "serious" : s === "medium" ? "warning" : "neutral";

export function News2Badge({ score, band, partial }: { score: number | null; band: string | null;
                                                       partial?: boolean | null }) {
  if (score == null) return <Badge status="neutral">no score</Badge>;
  return (
    <Badge status={bandStatus(band)} title={partial ? "Partial NEWS2: some parameters not measured" : undefined}>
      NEWS2 {score}{partial ? "*" : ""} · {band}
    </Badge>
  );
}

export function ago(ts: string | null | undefined, now = Date.now()): string {
  if (!ts) return "never";
  const s = Math.max(0, (now - new Date(ts).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export const hhmm = (ts: string | number | Date) =>
  new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export const fmt = (v: unknown, digits = 0) =>
  v == null || v === "" || Number.isNaN(Number(v)) ? "–" : Number(v).toFixed(digits);

/** 12-point trend line in the de-emphasis hue with the current point in the accent. */
export function Sparkline({ values, width = 84, height = 22, label }: { values: number[]; width?: number;
                                                                        height?: number; label: string }) {
  if (values.length < 2) return <span className="muted small">–</span>;
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const x = (i: number) => 2 + (i * (width - 6)) / (values.length - 1);
  const y = (v: number) => height - 4 - ((v - lo) / span) * (height - 8);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const last = values[values.length - 1];
  return (
    <svg width={width} height={height} role="img" aria-label={`${label} trend, latest ${last.toFixed(0)}`}>
      <path d={d} fill="none" stroke="var(--spark)" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(values.length - 1)} cy={y(last)} r="3" fill="var(--accent)" stroke="var(--surface)"
              strokeWidth="1.5" />
    </svg>
  );
}
