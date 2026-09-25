// Patient view: the active alert with the agent's cited SBAR brief, escalation timeline and notifications; live
// vitals small multiples; risk models (flagged when unvalidated); record summary; clinician + presenter actions.
import { Fragment, useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, type AlertDetail, type Chunk, type EventRow, type LiveMessage, type RiskRow, type VitalRow } from "../api";
import VitalsPanels from "../components/VitalsPanels";
import { ago, Badge, bandStatus, fmt, hhmm, News2Badge, severityStatus } from "../components/ui";

const TIER = ["", "RPM nurse", "On-call physician", "Rapid response team"];
const SCENARIOS = ["sepsis", "chf_decompensation", "copd_exacerbation", "hypoxia", "af_rvr", "bradycardia",
                   "motion_artifact", "device_offline", "fall", "sos"];
const REF = /\[([A-Za-z0-9_\-]+(?:#\d+)?)\]/g;

function withCitations(text: string, onCite: (ref: string) => void): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(REF)) {
    out.push(text.slice(last, m.index));
    const ref = m[1];
    out.push(ref.includes("#")
      ? <button key={m.index} className="cite" onClick={() => onCite(ref)} title="Show source">{ref}</button>
      : <span key={m.index} className="cite" style={{ cursor: "default" }} title="Live data block">{ref}</span>);
    last = (m.index ?? 0) + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

function Markdown({ text }: { text: string }) {
  return <div>{text.split("\n").map((ln, i) => ln.startsWith("## ")
    ? <h3 key={i} style={{ marginTop: 10 }}>{ln.slice(3)}</h3>
    : ln.startsWith("- ") ? <div key={i}>• {ln.slice(2)}</div> : <p key={i} style={{ margin: "4px 0" }}>{ln}</p>)}</div>;
}

export default function PatientView({ id, now, live, demo }: { id: string; now: number; live: LiveMessage | null;
                                                                demo: boolean }) {
  const [p, setP] = useState<Record<string, any> | null>(null);
  const [alert, setAlert] = useState<AlertDetail | null>(null);
  const [vitals, setVitals] = useState<VitalRow[]>([]);
  const [minutes, setMinutes] = useState(60);
  const [loadingVitals, setLoadingVitals] = useState(false);
  const [risk, setRisk] = useState<RiskRow[]>([]);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [summary, setSummary] = useState<{ title: string; content: string } | null>(null);
  const [evidence, setEvidence] = useState<Chunk | null>(null);
  const [table, setTable] = useState(false);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [scenario, setScenario] = useState("sepsis");
  const [ramp, setRamp] = useState(90);

  const loadAlert = useCallback(async () => {
    const active = (await api.alerts("active")).find((a) => a.patient_id === id);
    setAlert(active ? await api.alert(active.alert_id) : null);
  }, [id]);
  const loadSide = useCallback(async () => {
    const [pp, rk, ev] = await Promise.all([api.patient(id), api.risk(id), api.events(30, id)]);
    setP(pp); setRisk(rk); setEvents(ev);
  }, [id]);

  useEffect(() => {
    setEvidence(null);
    void loadAlert(); void loadSide();
    api.summary(id).then(setSummary).catch(() => setSummary(null));
  }, [id, loadAlert, loadSide]);
  useEffect(() => {
    setLoadingVitals(true);
    api.vitals(id, minutes).then((v) => setVitals(v.reverse())).finally(() => setLoadingVitals(false));
  }, [id, minutes]);

  useEffect(() => {  // live updates for this patient
    if (!live) return;
    if (live.type === "vitals" && live.patient_id === id && !live.late) {
      setVitals((v) => [...v, { ts: live.ts, ...live.vitals, news2: live.news2?.total ?? null,
        news2_band: live.news2?.band ?? null, news2_partial: live.news2?.partial ?? null, buffered: live.buffered,
        late: false } as VitalRow]);
      setP((pp) => pp && { ...pp, news2: live.news2?.total ?? pp.news2, news2_band: live.news2?.band ?? pp.news2_band,
                               news2_partial: live.news2?.partial ?? pp.news2_partial, last_seen: live.ts });
    } else if ((live.type === "alert" && live.alert.patient_id === id)
      || (live.type === "agent_run" && live.patient_id === id)
      || (live.type === "alert_changed" && (live.alert_id === alert?.alert_id || !alert))) {
      void loadAlert(); void loadSide();
    } else if (live.type === "event" && live.event.patient_id === id) {
      setEvents((e) => [live.event, ...e].slice(0, 30));
    }
  }, [live]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setMsg(null);
    try { await fn(); setMsg(done); await loadAlert(); } catch (e) { setMsg(`Failed: ${(e as Error).message}`); }
  };
  const cite = async (ref: string) => { try { setEvidence(await api.chunk(ref)); } catch { setEvidence(null); } };

  if (!p) return <div className="page">Loading…</div>;
  const brief = alert?.brief;
  const nextEsc = alert?.next_escalation_at ? Math.max(0, (new Date(alert.next_escalation_at).getTime() - now) / 1000) : null;

  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 12, justifyContent: "space-between" }}>
        <div className="row">
          <a href="#/">← Worklist</a>
          <h1 style={{ marginLeft: 8 }}>{p.display_name}</h1>
          <span className="muted">{p.age}{p.sex} · {p.primary_condition} · {p.care_setting === "icu" ? "ICU"
            : `home, discharged ${p.discharge_at ? new Date(p.discharge_at).toLocaleDateString() : "–"}`}
            {" "}· device {p.device_id ?? "none"} · last data {ago(p.last_seen, now)}</span>
        </div>
        <News2Badge score={p.news2} band={p.news2_band} partial={p.news2_partial} />
      </div>

      <div className="grid-patient">
        <div className="stack">
          <section className="card" aria-labelledby="alert-h">
            <header>
              <h2 id="alert-h">Active alert</h2>
              {alert && <div className="row">
                <Badge status={severityStatus(alert.severity)}>{alert.severity}</Badge>
                <span className="tag">{alert.status}</span>
                <span className="tag">tier {alert.tier}: {TIER[alert.tier]}</span>
                {nextEsc != null && alert.status !== "acknowledged" &&
                  <span className="tag">escalates in {Math.ceil(nextEsc)}s</span>}
              </div>}
            </header>
            {!alert && <div className="empty">No active alert for this patient.</div>}
            {alert && <div className="body">
              <div style={{ fontWeight: 600 }}>{alert.title}</div>
              <div className="muted small">opened {ago(alert.opened_at, now)} · {alert.event_count} event(s):
                {" "}{alert.event_types.join(", ")}</div>
              <div className="row" style={{ marginTop: 10 }}>
                <input type="text" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)}
                       aria-label="Acknowledgement note" style={{ minWidth: 220 }} />
                <button className="btn primary" disabled={alert.status === "acknowledged"}
                        onClick={() => act(() => api.ack(alert.alert_id, note || undefined), "Acknowledged")}>
                  Acknowledge</button>
                <button className="btn" onClick={() => act(() => api.escalate(alert.alert_id, note || "Escalated by clinician"),
                  "Escalated")}>Escalate</button>
                <button className="btn danger" onClick={() => {
                  const r = window.prompt("Resolution (required)", note || "Reviewed; patient stable");
                  if (r && r.length >= 3) void act(() => api.resolve(alert.alert_id, r), "Resolved");
                }}>Resolve</button>
                {msg && <span className="muted small">{msg}</span>}
              </div>

              <h3 style={{ marginTop: 16 }}>Agent brief (SBAR)</h3>
              {!brief && <div className="muted">Agent brief pending…</div>}
              {brief && <>
                <div className="row small" style={{ marginTop: 4 }}>
                  <span className="tag">{brief.generated_by ?? "agent"}</span>
                  {brief.citation_check && <Badge status={brief.citation_check === "passed" ? "good"
                    : brief.citation_check === "template" ? "neutral" : "warning"}>citations {brief.citation_check}</Badge>}
                </div>
                <div style={{ fontWeight: 600, marginTop: 8 }}>{brief.headline}</div>
                <dl className="sbar">
                  {(["situation", "background", "assessment", "recommendation"] as const).map((k) => (
                    <Fragment key={k}><dt>{k}</dt><dd>{withCitations(brief[k], cite)}</dd></Fragment>
                  ))}
                </dl>
                {brief.uncertainties.length > 0 && <div className="small" style={{ marginTop: 8 }}>
                  <span className="muted">Uncertainty: </span>{brief.uncertainties.join(" ")}</div>}
                {evidence && <div className="evidence" role="region" aria-label="Cited source">
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <b>{evidence.chunk_id} · {evidence.title} — {evidence.heading}</b>
                    <button className="btn" onClick={() => setEvidence(null)} aria-label="Close source">Close</button>
                  </div>{evidence.text}</div>}
              </>}

              {alert.agent_assessment && <>
                <h3 style={{ marginTop: 16 }}>Agent assessment</h3>
                <div className="row small" style={{ marginTop: 4 }}>
                  <Badge status={severityStatus(alert.agent_assessment.concern_level)}>
                    concern {alert.agent_assessment.concern_level}</Badge>
                  <span className="tag">signal {alert.agent_assessment.signal_quality.replace("_", " ")}</span>
                  <span className="tag">confidence {alert.agent_assessment.confidence}</span>
                </div>
                <p style={{ margin: "6px 0" }}>{alert.agent_assessment.clinical_picture}</p>
                <div className="muted small">{alert.agent_assessment.model_evidence_note}</div>
              </>}

              <h3 style={{ marginTop: 16 }}>Timeline</h3>
              <ul className="timeline">
                {alert.timeline.map((t, i) => (
                  <li key={i}><span className="muted small">{hhmm(t.at)}</span>
                    <div><div>{t.summary}</div><div className="actor">{t.kind.replace("_", " ")} · {t.actor}</div></div>
                  </li>))}
              </ul>
              <h3 style={{ marginTop: 16 }}>Notifications</h3>
              <table className="small">
                <thead><tr><th>Tier</th><th>Channel</th><th>Recipient</th><th>Status</th>
                  <th className="num" title="From what caused the page: sensor reading, escalation timer, agent or clinician request">Cause → sent</th></tr></thead>
                <tbody>{alert.notifications.map((n, i) => (
                  <tr key={i}><td>{n.tier}</td><td>{n.channel}</td><td>{n.recipient}</td>
                    <td>{n.status}{n.error ? ` (${n.error})` : ""}</td>
                    <td className="num">{n.latency_ms != null ? `${(n.latency_ms / 1000).toFixed(2)} s` : "–"}
                      {n.cause && n.cause !== "event" ? ` (${n.cause})` : ""}</td></tr>
                ))}</tbody>
              </table>
            </div>}
          </section>

          <section className="card" aria-labelledby="vitals-h">
            <header>
              <h2 id="vitals-h">Vital signs</h2>
              <div className="row">
                <div className="seg" role="group" aria-label="Time range">
                  {[60, 360, 1440].map((m) => <button key={m} aria-pressed={minutes === m}
                    onClick={() => setMinutes(m)}>{m === 60 ? "1 h" : m === 360 ? "6 h" : "24 h"}</button>)}
                </div>
                <button className="btn" aria-pressed={table} onClick={() => setTable(!table)}>
                  {table ? "Chart view" : "Table view"}</button>
              </div>
            </header>
            <div className="body">
              {table ? (
                <div className="scroll" style={{ maxHeight: 420 }}>
                  <table className="small"><thead><tr><th>Time</th><th className="num">HR</th><th className="num">SpO₂</th>
                    <th className="num">RR</th><th className="num">BP</th><th className="num">Temp</th>
                    <th className="num">NEWS2</th><th>Note</th></tr></thead>
                    <tbody>{[...vitals].reverse().slice(0, 400).map((v) => (
                      <tr key={v.ts}><td>{hhmm(v.ts)}</td><td className="num">{fmt(v.heart_rate)}</td>
                        <td className="num">{fmt(v.spo2)}</td><td className="num">{fmt(v.resp_rate)}</td>
                        <td className="num">{v.sbp != null ? `${fmt(v.sbp)}/${fmt(v.dbp)}` : "–"}</td>
                        <td className="num">{fmt(v.temp, 1)}</td><td className="num">{fmt(v.news2)}</td>
                        <td className="muted">{v.buffered ? "backfilled" : ""}{v.late ? " late" : ""}</td></tr>))}
                    </tbody></table></div>
              ) : <VitalsPanels rows={vitals} minutes={minutes} now={now} stale={loadingVitals} />}
            </div>
          </section>
        </div>

        <div className="stack">
          <section className="card" aria-labelledby="actions-h">
            <header><h2 id="actions-h">Actions</h2><span className="muted small">sent to the patient's device</span></header>
            <div className="body row">
              <button className="btn" disabled={p.care_setting !== "home"}
                      onClick={() => act(() => api.command(id, { cmd: "SPOT_CHECK" }), "SpO₂ spot check requested")}>
                Request SpO₂ reading</button>
              <button className="btn" disabled={p.care_setting !== "home"}
                      onClick={() => act(() => api.command(id, { cmd: "CHECKIN", topic: "breathless" }), "Check-in sent")}>
                Ask: short of breath?</button>
              <button className="btn" onClick={() => act(() => api.command(id, { cmd: "SET_PROFILE", profile: "elevated" }),
                "Sampling raised")}>Raise sampling</button>
            </div>
          </section>

          <section className="card" aria-labelledby="risk-h">
            <header><h2 id="risk-h">Risk models</h2><span className="muted small">decision support only</span></header>
            <div className="body">
              {risk.length === 0 && <div className="muted">Not scored yet (scored when the agent runs).</div>}
              {risk.map((r) => (
                <div key={r.model} style={{ marginBottom: 10 }}>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <b>{r.model.replace(/_/g, " ")}</b>
                    <Badge status={bandStatus(r.band)}>{(r.probability * 100).toFixed(0)}% · {r.band}</Badge>
                  </div>
                  <div className="muted small">v{r.model_version} · {ago(r.computed_at, now)}{" "}
                    {r.validated ? <span className="tag">validated</span>
                      : <span className="tag" title={`Cross-validated metrics: ${JSON.stringify(r.metrics)}`}>
                          unvalidated baseline — weak evidence</span>}</div>
                  <div className="small">{r.top_factors.slice(0, 3).map((f) =>
                    `${f.feature.replace(/_/g, " ")} ${f.contribution > 0 ? "↑" : "↓"}`).join(" · ")}</div>
                </div>))}
            </div>
          </section>

          {demo && <section className="card demo" aria-labelledby="demo-h">
            <header><h2 id="demo-h">Presenter controls</h2><span className="muted small">simulated patient only</span></header>
            <div className="body row">
              <select value={scenario} onChange={(e) => setScenario(e.target.value)} aria-label="Scenario">
                {SCENARIOS.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
              </select>
              <label className="small muted">ramp (s) <input type="number" min={5} max={900} value={ramp}
                style={{ width: 70 }} onChange={(e) => setRamp(Number(e.target.value))} /></label>
              <button className="btn" onClick={() => act(() => api.scenario({ patient_id: id, scenario, ramp_s: ramp }),
                `Injected ${scenario}`)}>Inject</button>
            </div>
          </section>}

          <section className="card" aria-labelledby="events-h">
            <header><h2 id="events-h">Clinical events</h2><span className="muted small">last 30</span></header>
            <div className="scroll" style={{ maxHeight: 320 }}>
              {events.length === 0 && <div className="empty">No events.</div>}
              <ul className="timeline" style={{ padding: "0 14px" }}>
                {events.map((e) => (
                  <li key={e.event_id}><span className="muted small">{hhmm(e.detected_at)}</span>
                    <div><Badge status={severityStatus(e.severity)}>{e.type}</Badge>
                      <div className="small">{e.summary}</div></div></li>))}
              </ul>
            </div>
          </section>

          {summary && <section className="card" aria-labelledby="sum-h">
            <header><h2 id="sum-h">Record summary</h2><span className="muted small">from the structured record</span></header>
            <div className="body small scroll" style={{ maxHeight: 360 }}><Markdown text={summary.content} /></div>
          </section>}
        </div>
      </div>
    </div>
  );
}
