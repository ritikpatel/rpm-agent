// Worklist: stat tiles, the alert inbox (what needs a human now) and the patient census (everyone, risk-sorted).
import { useMemo, useState } from "react";
import { api, type AlertRow, type PatientRow, type Stats } from "../api";
import { ago, Badge, bandStatus, fmt, News2Badge, severityStatus, Sparkline } from "../components/ui";

const TIER = ["", "RPM nurse", "Physician", "Rapid response"];
const BAND_RANK: Record<string, number> = { low: 0, medium: 1, high: 2 };

/** The most concerning risk model for a patient, as a compact badge. */
function topRisk(p: PatientRow) {
  const entries = Object.entries(p.risk ?? {});
  if (entries.length === 0) return <span className="muted small">–</span>;
  const [model, r] = entries.sort((a, b) => (BAND_RANK[b[1].band] ?? 0) - (BAND_RANK[a[1].band] ?? 0)
    || b[1].probability - a[1].probability)[0];
  // Each badge names what is predicted ("Poor recovery 98%"), so a high number cannot be read as good news.
  return <Badge status={bandStatus(r.band)} title={entries.map(([m, x]) =>
    `${x.label ?? m.replace(/_/g, " ")}: ${(x.probability * 100).toFixed(0)}% (${x.band})`).join("\n")}>
    {r.short_label ?? model.split("_")[0]} {(r.probability * 100).toFixed(0)}%</Badge>;
}

export default function Worklist({ patients, alerts, stats, trends, flashed, now, onChange, canAct }: {
  patients: PatientRow[]; alerts: AlertRow[]; stats: Stats | null; trends: Record<string, number[]>;
  flashed: Set<string>; now: number; onChange: () => void; canAct: boolean;
}) {
  const [setting, setSetting] = useState<"all" | "home" | "icu">("all");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const rows = useMemo(() => patients
    .filter((p) => setting === "all" || p.care_setting === setting)
    .filter((p) => !query || `${p.display_name} ${p.patient_id} ${p.primary_diagnosis ?? ""}`.toLowerCase()
      .includes(query.toLowerCase()))
    .sort((a, b) => (b.news2 ?? -1) - (a.news2 ?? -1) || a.patient_id.localeCompare(b.patient_id)),
  [patients, setting, query]);
  const visibleAlerts = alerts.filter((a) => setting === "all" || a.care_setting === setting);

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
        <div className="card tile"><div className="label">Active alerts</div>
          <div className="value">{alerts.length}</div>
          <div className="sub">{unacked.length} awaiting acknowledgement</div></div>
        <div className="card tile"><div className="label">Critical alerts</div>
          <div className="value">{alerts.filter((a) => a.severity === "critical").length}</div>
          <div className="sub">{alerts.filter((a) => a.severity === "high").length} high</div></div>
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
        <section className="card" aria-labelledby="inbox-h">
          <header><h2 id="inbox-h">Alert inbox</h2><span className="muted small">{visibleAlerts.length} active</span></header>
          <div className="scroll">
            {visibleAlerts.length === 0 && <div className="empty">No active alerts.</div>}
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
              <thead><tr><th>Patient</th><th>Status</th><th title="Highest band among the risk models">Risk</th>
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
                      <td>{topRisk(p)}</td>
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
