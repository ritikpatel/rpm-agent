// Worklist: stat tiles, the alert inbox (what needs a human now) and the patient census (everyone, risk-sorted).
import { useMemo, useRef, useState } from "react";
import { api, type AlertRow, type PatientRow, type Stats } from "../api";
import { agreement, ago, Badge, bandStatus, fmt, News2Badge, severityStatus, Sparkline, toneStatus } from "../components/ui";

const TIER = ["", "RPM nurse", "Physician", "Rapid response"];

/** The short-term models (recovery, deterioration): whether they agree, each one's own reading in the tooltip. */
function nextDays(p: PatientRow) {
  const models = Object.entries(p.risk ?? {}).filter(([, r]) => r.horizon !== "long");
  if (models.length === 0) return <span className="muted small">–</span>;
  const tip = models.map(([m, r]) => r.badge ?? `${m.replace(/_/g, " ")} ${(r.probability * 100).toFixed(0)}%`).join("\n");
  const agree = agreement(models.map(([m, r]) => ({ name: r.short_label ?? m, band: r.band, validated: r.validated })));
  if (!agree) {   // one model so far: its own short form
    const [m, r] = models[0];
    return <Badge status={r.tone ? toneStatus(r.tone) : bandStatus(r.band)} title={tip}>
      {r.badge ?? `${m.split("_")[0]} ${(r.probability * 100).toFixed(0)}%`}</Badge>;
  }
  return <Badge status={agree.status} title={`${tip}\n\n${agree.text}`}>{agree.label}</Badge>;
}

/** The 30-day readmission model, in its own short form ("Readmission 28%"). */
function thirtyDays(p: PatientRow) {
  const r = Object.values(p.risk ?? {}).find((x) => x.horizon === "long");
  if (!r) return <span className="muted small">–</span>;
  return <Badge status={r.tone ? toneStatus(r.tone) : bandStatus(r.band)}
    title="Research model: readmission within 30 days. It never raises or confirms an alert.">
    {r.badge ?? `${(r.probability * 100).toFixed(0)}%`}</Badge>;
}

export default function Worklist({ patients, alerts, stats, trends, flashed, now, onChange, canAct }: {
  patients: PatientRow[]; alerts: AlertRow[]; stats: Stats | null; trends: Record<string, number[]>;
  flashed: Set<string>; now: number; onChange: () => void; canAct: boolean;
}) {
  const [setting, setSetting] = useState<"all" | "home" | "icu">("all");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [severity, setSeverity] = useState<"all" | "critical">("all");   // set from the alert tiles
  const inbox = useRef<HTMLElement>(null);
  const showAlerts = (which: "all" | "critical") => {
    setSeverity(which);
    inbox.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const rows = useMemo(() => patients
    .filter((p) => setting === "all" || p.care_setting === setting)
    .filter((p) => !query || `${p.display_name} ${p.patient_id} ${p.primary_diagnosis ?? ""}`.toLowerCase()
      .includes(query.toLowerCase()))
    .sort((a, b) => (b.news2 ?? -1) - (a.news2 ?? -1) || a.patient_id.localeCompare(b.patient_id)),
  [patients, setting, query]);
  const visibleAlerts = alerts.filter((a) => (setting === "all" || a.care_setting === setting)
    && (severity === "all" || a.severity === "critical"));

  const ack = async (a: AlertRow) => {
    setBusy(a.alert_id);
    try { await api.ack(a.alert_id); onChange(); } finally { setBusy(null); }
  };
  const unacked = alerts.filter((a) => a.status !== "acknowledged");
  const alertsHour = Object.entries(stats?.events_1h ?? {})
    .filter(([k]) => ["medium", "high", "critical"].includes(k)).reduce((s, [, n]) => s + n, 0);

  return (
    <div className="page">
      <div className="tiles">
        <div className="card tile"><div className="label">Patients monitored</div>
          <div className="value">{stats?.patients ?? "–"}</div>
          <div className="sub">{stats?.icu ?? "–"} in ICU · {stats?.home ?? "–"} at home · {stats?.streaming ?? "–"} streaming</div></div>
        <button className="card tile link" onClick={() => showAlerts("all")}
                title="Show every active alert in the inbox"><div className="label">Active alerts</div>
          <div className="value">{alerts.length}</div>
          <div className="sub">{unacked.length} awaiting acknowledgement</div></button>
        <button className="card tile link" onClick={() => showAlerts("critical")} aria-pressed={severity === "critical"}
                title="Show only the critical alerts in the inbox"><div className="label">Critical alerts</div>
          <div className="value">{alerts.filter((a) => a.severity === "critical").length}</div>
          <div className="sub">{alerts.filter((a) => a.severity === "high").length} high</div></button>
        <div className="card tile"><div className="label">Sensor → page, p95</div>
          <div className="value">{stats?.page_p95_s != null ? `${stats.page_p95_s.toFixed(2)} s` : "–"}</div>
          <div className="sub">{stats?.pages_1h ?? 0} pages · {alertsHour} rule events, last hour</div></div>
        <div className="card tile"><div className="label">Sensor → scored, p95</div>
          <div className="value">{stats?.e2e_p95_s != null ? `${stats.e2e_p95_s.toFixed(2)} s` : "–"}</div>
          <div className="sub">p50 {stats?.e2e_p50_s != null ? `${stats.e2e_p50_s.toFixed(2)} s` : "–"}, last 5 min</div></div>
      </div>

      <div className="filters" role="toolbar" aria-label="Filters">
        <div className="seg" role="group" aria-label="Care setting">
          {(["all", "home", "icu"] as const).map((s) => (
            <button key={s} aria-pressed={setting === s} onClick={() => setSetting(s)}>
              {s === "all" ? "All patients" : s === "home" ? "Post-discharge" : "ICU"}
            </button>
          ))}
        </div>
        <input type="search" placeholder="Search name, id, diagnosis" value={query}
               onChange={(e) => setQuery(e.target.value)} aria-label="Search patients" />
      </div>

      <div className="grid-2">
        <section className="card" aria-labelledby="inbox-h" ref={inbox} style={{ scrollMarginTop: 12 }}>
          <header><h2 id="inbox-h">Alert inbox</h2>
            {severity === "critical"
              ? <span className="row small">critical only · {visibleAlerts.length}
                  <button className="btn" onClick={() => setSeverity("all")}>Show all</button></span>
              : <span className="muted small">{visibleAlerts.length} active</span>}</header>
          <div className="scroll">
            {visibleAlerts.length === 0 && <div className="empty">
              {severity === "critical" ? "No critical alerts." : "No active alerts."}</div>}
            {visibleAlerts.map((a) => (
              <div className="alert-item" key={a.alert_id} role="link" tabIndex={0}
                   onClick={() => { location.hash = `#/patients/${a.patient_id}`; }}
                   onKeyDown={(e) => { if (e.key === "Enter") location.hash = `#/patients/${a.patient_id}`; }}>
                <Badge status={severityStatus(a.severity)}>{a.severity}</Badge>
                <div className="title">{a.display_name} <span className="muted small">· {a.primary_condition} ·
                  {a.care_setting === "icu" ? " ICU" : " home"}</span></div>
                <div className="actions">
                  {a.status === "acknowledged"
                    ? <span className="tag">ack {a.acknowledged_by}</span>
                    : canAct && <button className="btn" disabled={busy === a.alert_id}
                              onClick={(e) => { e.stopPropagation(); void ack(a); }}>Acknowledge</button>}
                </div>
                <div className="meta">{a.title} · {a.event_count} event{a.event_count > 1 ? "s" : ""} ·
                  opened {ago(a.opened_at, now)} · tier {a.tier} ({TIER[a.tier] ?? a.tier})
                  {a.status === "escalated" ? " · escalated" : ""}</div>
                <div className="brief">{a.brief_headline
                  ? <>Brief: {a.brief_headline} {a.brief_status === "offline" && <span className="tag">offline rules</span>}</>
                  : <span className="muted">Agent brief pending…</span>}</div>
              </div>
            ))}
          </div>
        </section>

        <section className="card" aria-labelledby="census-h">
          <header><h2 id="census-h">Patient census</h2><span className="muted small">{rows.length} patients,
            sorted by NEWS2</span></header>
          <div className="scroll">
            <table>
              <thead><tr><th>Patient</th><th>Status</th><th title="Recovery and deterioration: whether they are at or above their alert thresholds">Next days</th>
                <th title="Readmission within 30 days (research model)">30 days</th>
                <th>HR trend</th><th className="num">HR</th><th className="num">SpO₂</th><th>Last seen</th></tr></thead>
              <tbody>
                {rows.map((p) => {
                  const L = p.latest ?? {};
                  const stale = !p.last_seen || now - new Date(p.last_seen).getTime() > 120000;
                  return (
                    <tr key={p.patient_id} className={`clickable${flashed.has(p.patient_id) ? " flash" : ""}`}
                        onClick={() => { location.hash = `#/patients/${p.patient_id}`; }}>
                      <td><div>{p.display_name}</div><div className="muted small">{p.patient_id} · {p.age}{p.sex} ·
                        {" "}{p.care_setting === "icu" ? "ICU" : "home"}
                        {p.primary_diagnosis ? ` · ${p.primary_diagnosis.slice(0, 44)}` : ""}</div></td>
                      <td><News2Badge score={p.news2} band={p.news2_band} partial={p.news2_partial} /></td>
                      <td>{nextDays(p)}</td>
                      <td>{thirtyDays(p)}</td>
                      <td><Sparkline values={trends[p.patient_id] ?? []} label={`${p.display_name} heart rate`} /></td>
                      <td className="num">{fmt(L.heart_rate?.v)}</td>
                      <td className="num">{fmt(L.spo2?.v)}</td>
                      <td className={stale ? "muted" : ""}>{ago(p.last_seen, now)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="body muted small">* partial NEWS2: the wearable cannot measure every parameter.</div>
        </section>
      </div>
    </div>
  );
}
