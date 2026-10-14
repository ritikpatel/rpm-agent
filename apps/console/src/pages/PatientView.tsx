// Patient view: the active alert with the agents' cited SBAR summary, escalation timeline and notifications; live
// vitals; the three risk models (flagged when unvalidated); labs; messages with the patient; record summary;
// clinician actions and demonstration controls (scenario, discharge / admit, lab result).
import { Fragment, useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, type AgentRun, type AlertDetail, type Brief, type Chunk, type EventRow, type LabRow, type LiveMessage,
         type MessageRow, type RiskRow, type VitalRow } from "../api";
import VitalsPanels from "../components/VitalsPanels";
import { ago, Badge, bandStatus, fmt, hhmm, News2Badge, severityStatus, toneStatus } from "../components/ui";

const TIER = ["", "RPM nurse", "On-call physician", "Rapid response team"];
export const SCENARIOS = ["sepsis", "chf_decompensation", "copd_exacerbation", "hypoxia", "af_rvr", "bradycardia",
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

export default function PatientView({ id, now, live, demo, canAct }: { id: string; now: number;
                                                                        live: LiveMessage | null; demo: boolean;
                                                                        canAct: boolean }) {
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
  // the newest reading that came from the patient's record rather than from a device (rows are oldest first)
  const lastRecorded = [...vitals].reverse().find((v) => v.source === "record");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [scenario, setScenario] = useState("sepsis");
  const [ramp, setRamp] = useState(60);
  const [labs, setLabs] = useState<LabRow[]>([]);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [text, setText] = useState("");
  const [watch, setWatch] = useState("");
  const [lab, setLab] = useState({ name: "Lactate", value: "4.2", unit: "mmol/L", flag: "H" });

  const loadAlert = useCallback(async () => {
    const active = (await api.alerts("active")).find((a) => a.patient_id === id);
    setAlert(active ? await api.alert(active.alert_id) : null);
  }, [id]);
  const loadSide = useCallback(async () => {
    const [pp, rk, ev, lb, ms, rn] = await Promise.all([api.patient(id), api.risk(id), api.events(30, id),
      api.labs(id), api.messages(id), api.agentRuns(id)]);
    setP(pp); setRisk(rk); setEvents(ev); setLabs(lb); setMessages(ms); setRuns(rn);
  }, [id]);
  const loadVitals = useCallback(() => {
    setLoadingVitals(true);
    api.vitals(id, minutes).then((v) => setVitals(v.reverse())).finally(() => setLoadingVitals(false));
  }, [id, minutes]);

  useEffect(() => {
    setEvidence(null);
    void loadAlert(); void loadSide();
    api.summary(id).then(setSummary).catch(() => setSummary(null));
  }, [id, loadAlert, loadSide]);
  useEffect(() => { loadVitals(); }, [loadVitals]);

  useEffect(() => {  // live updates for this patient
    if (!live) return;
    if (live.type === "vitals" && live.patient_id === id && !live.late) {
      setVitals((v) => [...v, { ts: live.ts, ...live.vitals, news2: live.news2?.total ?? null,
        news2_band: live.news2?.band ?? null, news2_partial: live.news2?.partial ?? null, buffered: live.buffered,
        late: false } as VitalRow]);
      setP((pp) => pp && { ...pp, news2: live.news2?.total ?? pp.news2, news2_band: live.news2?.band ?? pp.news2_band,
                               news2_partial: live.news2?.partial ?? pp.news2_partial, last_seen: live.ts });
    } else if ((live.type === "alert" && live.alert?.patient_id === id)
      || (live.type === "agent_run" && live.patient_id === id)
      || (live.type === "alert_changed" && (live.alert_id === alert?.alert_id || !alert))) {
      void loadAlert(); void loadSide();
    } else if ((live.type === "message" || live.type === "risk") && live.patient_id === id) {
      void loadSide();
    } else if (live.type === "event" && live.event.patient_id === id) {
      setEvents((e) => [live.event, ...e].slice(0, 30));
      if (["DISCHARGED", "ADMITTED", "LAB_RESULT"].includes(live.event.type)) {
        // the care setting (and with it the device) just changed: reload after the registry has caught up
        window.setTimeout(() => { void loadSide(); loadVitals(); }, 1500);
      }
    }
  }, [live]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setMsg(null);
    try { await fn(); setMsg(done); await loadAlert(); await loadSide(); }
    catch (e) { setMsg(`Failed: ${(e as Error).message}`); }
  };
  const cite = async (ref: string) => { try { setEvidence(await api.chunk(ref)); } catch { setEvidence(null); } };

  if (!p) return <div className="page">Loading…</div>;
  const brief = alert?.brief;
  const lastRun = runs.find((r) => r.brief) ?? null;   // newest agent summary, shown when no alert is open
  const home = p.care_setting === "home";
  const nextEsc = alert?.next_escalation_at ? Math.max(0, (new Date(alert.next_escalation_at).getTime() - now) / 1000) : null;

  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 12, justifyContent: "space-between" }}>
        <div className="row">
          <a href="#/">← Worklist</a>
          <h1 style={{ marginLeft: 8 }}>{p.display_name}</h1>
          <span className="muted">{p.patient_id} · {p.age}{p.sex} · {p.primary_diagnosis ?? "no diagnosis on record"}
            {" "}· {p.data_origin === "mimic_real" ? "MIMIC patient" : "synthetic patient"} · {p.care_setting === "icu" ? "ICU"
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
            {!alert && <div className="body">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="muted">No active alert for this patient.</span>
                {canAct && <button className="btn" onClick={() => act(() => api.agentReview(id),
                  "Agent review requested; the summary appears here when it is ready")}>Ask the agents for a review</button>}
              </div>
              {msg && <div className="muted small" style={{ marginTop: 6 }}>{msg}</div>}
              {lastRun?.brief && <>
                <h3 style={{ marginTop: 14 }}>Latest agent summary <span className="muted small">
                  · {lastRun.trigger.replace("_", " ")} · {ago(lastRun.started_at, now)}</span></h3>
                <BriefView brief={lastRun.brief} cite={cite} />
                {evidence && <Evidence chunk={evidence} onClose={() => setEvidence(null)} />}
              </>}
            </div>}
            {alert && <div className="body">
              <div style={{ fontWeight: 600 }}>{alert.title}</div>
              <div className="muted small">opened {ago(alert.opened_at, now)} · {alert.event_count} event(s):
                {" "}{alert.event_types.join(", ")}</div>
              {canAct && <div className="row" style={{ marginTop: 10 }}>
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
              </div>}

              <h3 style={{ marginTop: 16 }}>Agent summary (SBAR)</h3>
              {!brief && <div className="muted">The agents are working on it…</div>}
              {brief && <>
                <BriefView brief={brief} cite={cite} />
                {evidence && <Evidence chunk={evidence} onClose={() => setEvidence(null)} />}
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
              {lastRecorded && <p className="small muted">Readings up to {hhmm(lastRecorded.ts)} are from the
                patient's record; later ones are live.</p>}
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
                        <td className="muted">{v.source === "record" ? "recorded" : ""}{v.buffered ? "backfilled" : ""}{v.late ? " late" : ""}</td></tr>))}
                    </tbody></table></div>
              ) : <VitalsPanels rows={vitals} minutes={minutes} now={now} stale={loadingVitals} />}
            </div>
          </section>
        </div>

        <div className="stack">
          {canAct && <section className="card" aria-labelledby="actions-h">
            <header><h2 id="actions-h">Actions</h2><span className="muted small">sent to the patient's device</span></header>
            <div className="body row">
              <button className="btn" disabled={!home} title={home ? undefined : "ICU monitors have no patient screen"}
                      onClick={() => act(() => api.command(id, { cmd: "SPOT_CHECK" }), "SpO₂ spot check requested")}>
                Request SpO₂ reading</button>
              <button className="btn" disabled={!home} title={home ? undefined : "ICU monitors have no patient screen"}
                      onClick={() => act(() => api.command(id, { cmd: "CHECKIN", topic: "breathless" }), "Check-in sent")}>
                Ask: short of breath?</button>
              <button className="btn" onClick={() => act(() => api.command(id, { cmd: "SET_PROFILE", profile: "elevated" }),
                "Sampling raised")}>Raise sampling</button>
              {alert && msg && <span className="muted small">{msg}</span>}
            </div>
          </section>}

          {home && <section className="card" aria-labelledby="msg-h">
            <header><h2 id="msg-h">Messages with the patient</h2><span className="muted small">shown on the watch</span></header>
            <div className="body">
              {canAct && <form className="row" onSubmit={(e) => { e.preventDefault();
                if (text.trim()) void act(() => api.sendMessage(id, text.trim()).then(() => setText("")), "Message sent"); }}>
                <input type="text" maxLength={200} value={text} onChange={(e) => setText(e.target.value)}
                       placeholder="Short message to the patient" aria-label="Message" style={{ flex: 1, minWidth: 180 }} />
                <button className="btn primary" disabled={!text.trim()}>Send</button>
              </form>}
              {messages.length === 0 && <div className="muted small" style={{ marginTop: 6 }}>No messages yet.</div>}
              <ul className="timeline" style={{ marginTop: 8 }}>
                {messages.slice(0, 8).map((m, i) => (
                  <li key={i}><span className="muted small">{hhmm(m.at)}</span>
                    <div><div>{m.direction === "from_patient" ? "Patient: " : ""}{m.text}</div>
                      <div className="actor">{m.kind} · {m.sender}</div></div></li>))}
              </ul>
            </div>
          </section>}

          <section className="card" aria-labelledby="risk-h">
            <header><h2 id="risk-h">Risk models</h2><span className="muted small">decision support only</span></header>
            <div className="body">
              {risk.length === 0 && <div className="muted">Not scored yet (scores follow the first readings).</div>}
              {risk.map((r) => {
                const o = r.output;
                const tag = r.validated ? <span className="tag">validated</span>
                  : <span className="tag" title={`Metrics: ${JSON.stringify(r.metrics)}`}>not validated: weak evidence</span>;
                if (!o) {   // a model without its own explanation: the probability of the outcome it names
                  return (
                    <div key={r.model} style={{ marginBottom: 12 }}>
                      <div className="row" style={{ justifyContent: "space-between" }}>
                        <b>{r.label ?? r.title ?? r.model}</b>
                        <Badge status={bandStatus(r.band)}>{(r.probability * 100).toFixed(0)}% · {r.band}</Badge>
                      </div>
                      <div className="muted small">{r.outcome}</div>
                      <div className="muted small">v{r.version} · {ago(r.computed_at, now)} {tag}</div>
                    </div>);
                }
                // The model's own reading, as its owner defined it (recovery score, readmission probability, ...).
                return (
                <div key={r.model} style={{ marginBottom: 14 }}>
                  <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
                    <b title={r.outcome ?? undefined}>{r.label ?? r.title}</b>
                    <Badge status={toneStatus(o.headline.tone)}>{o.headline.status}</Badge>
                  </div>
                  <div style={{ fontSize: "1.25em", margin: "2px 0" }}>
                    {o.headline.label}: <b>{o.headline.value ?? "–"}</b>{o.headline.suffix ? ` ${o.headline.suffix}` : ""}
                    {o.headline.trend && <span className="small muted"> · {o.headline.trend}</span>}
                  </div>
                  {(o.details ?? []).map((d) => <div key={d} className="small">{d}</div>)}
                  {(o.concerns ?? []).length > 0 &&
                    <div className="small">Concerns: {(o.concerns ?? []).join(" · ")}</div>}
                  {(o.reassurances ?? []).length > 0 &&
                    <div className="small muted">Reassuring: {(o.reassurances ?? []).join(" · ")}</div>}
                  {o.note && <div className="small muted" style={{ fontStyle: "italic" }}>{o.note}</div>}
                  <div className="muted small">{r.title} v{r.version} ({r.owner ?? "team"}) · {ago(r.computed_at, now)} {tag}</div>
                </div>);
              })}
            </div>
          </section>

          <section className="card" aria-labelledby="labs-h">
            <header><h2 id="labs-h">Lab results</h2><span className="muted small">from the hospital record (FHIR)</span></header>
            <div className="scroll" style={{ maxHeight: 200 }}>
              {labs.length === 0 && <div className="empty">No lab results received since monitoring started.</div>}
              {labs.length > 0 && <table className="small"><thead><tr><th>Time</th><th>Test</th>
                <th className="num">Value</th><th>Flag</th></tr></thead>
                <tbody>{labs.slice(0, 12).map((l, i) => (
                  <tr key={i}><td>{hhmm(l.ts)}</td><td>{l.name}</td>
                    <td className="num">{fmt(l.value, 1)} {l.unit ?? ""}</td><td>{l.flag ?? ""}</td></tr>))}</tbody></table>}
            </div>
          </section>

          {canAct && home && <section className="card" aria-labelledby="dev-h">
            <header><h2 id="dev-h">Watch</h2><span className="muted small">currently {p.device_id ?? "none"}</span></header>
            <div className="body row">
              <input type="text" value={watch} onChange={(e) => setWatch(e.target.value.trim())}
                     placeholder="Device id, e.g. gw-demo-01" aria-label="Watch device id" style={{ minWidth: 180 }} />
              <button className="btn" disabled={!/^[A-Za-z0-9_-]{3,64}$/.test(watch)}
                      onClick={() => act(() => api.assignDevice(id, watch), `Watch ${watch} assigned`)}>
                Assign watch</button>
            </div>
          </section>}

          {demo && <section className="card demo" aria-labelledby="demo-h">
            <header><h2 id="demo-h">Demo controls</h2><span className="muted small">simulated patient · hospital record</span></header>
            <div className="body stack" style={{ gap: 10 }}>
              <div className="row">
                <select value={scenario} onChange={(e) => setScenario(e.target.value)} aria-label="Scenario">
                  {SCENARIOS.map((sc) => <option key={sc} value={sc}>{sc.replace(/_/g, " ")}</option>)}
                </select>
                <label className="small muted">ramp (s) <input type="number" min={5} max={900} value={ramp}
                  style={{ width: 70 }} onChange={(e) => setRamp(Number(e.target.value))} /></label>
                <button className="btn" onClick={() => act(() => api.scenario({ patient_id: id, scenario, ramp_s: ramp }),
                  `Started ${scenario.replace(/_/g, " ")}`)}>Start scenario</button>
                <button className="btn" onClick={() => act(() => api.clearScenario(id), "Scenario ended")}>End</button>
              </div>
              <div className="row">
                {home
                  ? <button className="btn" onClick={() => act(() => api.admit(id), "Admitted to ICU in the hospital record")}>
                      Admit to ICU</button>
                  : <button className="btn" onClick={() => act(() => api.discharge(id),
                      "Discharged in the hospital record; the ICU monitor stops and a wearable starts")}>
                      Discharge home</button>}
                <span className="muted small">writes a FHIR Encounter; the platform reacts to the record</span>
              </div>
              <div className="row">
                <input type="text" value={lab.name} aria-label="Lab test" style={{ width: 100 }}
                       onChange={(e) => setLab({ ...lab, name: e.target.value })} />
                <input type="number" value={lab.value} aria-label="Lab value" style={{ width: 70 }}
                       onChange={(e) => setLab({ ...lab, value: e.target.value })} />
                <input type="text" value={lab.unit} aria-label="Lab unit" style={{ width: 70 }}
                       onChange={(e) => setLab({ ...lab, unit: e.target.value })} />
                <select value={lab.flag} aria-label="Lab flag" onChange={(e) => setLab({ ...lab, flag: e.target.value })}>
                  {["H", "HH", "L", "LL", "N"].map((f) => <option key={f}>{f}</option>)}
                </select>
                <button className="btn" disabled={!lab.name || lab.value === ""}
                        onClick={() => act(() => api.lab({ patient_id: id, name: lab.name, value: Number(lab.value),
                          unit: lab.unit || undefined, flag: lab.flag }), "Lab result filed in the hospital record")}>
                  File lab result</button>
              </div>
              {msg && <span className="muted small">{msg}</span>}
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


function BriefView({ brief, cite }: { brief: Brief; cite: (ref: string) => void }) {
  return (
    <>
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
    </>
  );
}

function Evidence({ chunk, onClose }: { chunk: Chunk; onClose: () => void }) {
  return (
    <div className="evidence" role="region" aria-label="Cited source">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <b>{chunk.chunk_id} · {chunk.title} — {chunk.heading}</b>
        <button className="btn" onClick={onClose} aria-label="Close source">Close</button>
      </div>{chunk.text}</div>
  );
}
