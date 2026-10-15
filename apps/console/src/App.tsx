import { useCallback, useEffect, useRef, useState } from "react";
import { api, connectLive, type AlertRow, type LiveMessage, type PatientRow, type Stats } from "./api";
import { completeSignIn, loadConfig, onAuthChange, signedIn, signOut, type AuthConfig, type Me } from "./auth";
import SignIn from "./components/SignIn";
import Audit from "./pages/Audit";
import PatientView, { SCENARIOS } from "./pages/PatientView";
import Worklist from "./pages/Worklist";

function useHashRoute() {
  const [hash, setHash] = useState(location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

type Theme = "system" | "light" | "dark";

/** Sign-in gate: completes an OIDC redirect, shows the sign-in screen, then the console for the user's role. */
export default function App() {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string>();
  const [, rerender] = useState(0);

  useEffect(() => onAuthChange(() => { setMe(null); rerender((n) => n + 1); }), []);
  useEffect(() => {
    completeSignIn().catch((e) => setError(String(e instanceof Error ? e.message : e)))
      .finally(() => loadConfig().then(setConfig).catch(() => setError("API unreachable")));
  }, []);
  const authed = signedIn();
  useEffect(() => {
    if (authed && config && !me) api.me().then(setMe).catch((e) => setError(String(e)));
  }, [authed, config, me]);

  if (!config) return <div className="page muted">{error ?? "Loading…"}</div>;
  if (!authed) return <SignIn config={config} error={error} />;
  if (!me) return <div className="page muted">Signing in…</div>;
  if (!me.can.read) return <PresenterOnly me={me} />;
  return <Console me={me} />;
}

function UserMenu({ me }: { me: Me }) {
  return (
    <span className="small muted">{me.title ?? "Care team"}{" "}
      <button className="btn" onClick={() => { void signOut(); }}>Sign out</button></span>
  );
}

/** A presenter without clinical access: can drive the demo, cannot see patient data (RBAC in the API too). */
function PresenterOnly({ me }: { me: Me }) {
  const [pid, setPid] = useState("");
  const [scenario, setScenario] = useState(SCENARIOS[0]);
  const [msg, setMsg] = useState("");
  return (
    <>
      <header className="topbar"><div className="brand"><h1>RPM Console</h1></div><div className="spacer" />
        <UserMenu me={me} /></header>
      <div className="page" style={{ maxWidth: 640 }}>
        <section className="card demo">
          <header><h2>Demo controls</h2><span className="muted small">no access to patient data</span></header>
          <div className="body row">
            <input value={pid} onChange={(e) => setPid(e.target.value.trim())} placeholder="SYN-000143"
                   aria-label="Patient id" />
            <select value={scenario} onChange={(e) => setScenario(e.target.value)} aria-label="Scenario">
              {SCENARIOS.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
            </select>
            <button className="btn" disabled={!/^[A-Za-z0-9-]{3,40}$/.test(pid)} onClick={() => {
              api.scenario({ patient_id: pid, scenario, ramp_s: 60 }).then(() => setMsg(`Started ${scenario}`),
                (e) => setMsg(String(e)));
            }}>Start scenario</button>
            {msg && <span className="muted small">{msg}</span>}
          </div>
        </section>
      </div>
    </>
  );
}

function Console({ me }: { me: Me }) {
  const route = useHashRoute();
  const [patients, setPatients] = useState<PatientRow[]>([]);
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [trends, setTrends] = useState<Record<string, number[]>>({});
  const [flashed, setFlashed] = useState<Set<string>>(new Set());
  const [live, setLive] = useState<LiveMessage | null>(null);
  const [up, setUp] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [theme, setTheme] = useState<Theme>(() => {
    try { return (localStorage.getItem("rpm.theme") as Theme) || "system"; } catch { return "system"; }
  });
  const alertTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("rpm.theme", theme); } catch { /* ignore */ }
  }, [theme]);

  const loadAlerts = useCallback(() => {
    window.clearTimeout(alertTimer.current);
    alertTimer.current = window.setTimeout(() => { api.alerts().then(setAlerts).catch(() => {}); }, 250);
  }, []);

  useEffect(() => {
    const census = () => api.patients().then(setPatients).catch(() => {});
    void census();
    api.alerts().then(setAlerts).catch(() => {});
    const refresh = () => api.stats().then(setStats).catch(() => {});
    void refresh();
    const s = window.setInterval(refresh, 5000);
    const c = window.setInterval(census, 20000);  // picks up admissions, discharges and new risk scores
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    const stop = connectLive((m) => {
      setLive(m);
      if (m.type === "vitals" && !m.late) {
        setPatients((ps) => ps.map((p) => p.patient_id !== m.patient_id ? p : {
          ...p, last_seen: m.ts, device_id: m.device_id,
          latest: { ...(p.latest ?? {}), ...Object.fromEntries(Object.entries(m.vitals).map(([k, v]) => [k, { v, ts: m.ts }])) },
          news2: m.news2?.total ?? p.news2, news2_band: m.news2?.band ?? p.news2_band,
          news2_partial: m.news2?.partial ?? p.news2_partial }));
        if (m.vitals.heart_rate != null) {
          setTrends((tr) => ({ ...tr, [m.patient_id]: [...(tr[m.patient_id] ?? []), m.vitals.heart_rate].slice(-12) }));
        }
        setFlashed((f) => new Set(f).add(m.patient_id));
        window.setTimeout(() => setFlashed((f) => { const n = new Set(f); n.delete(m.patient_id); return n; }), 900);
      } else if (m.type === "alert" || m.type === "alert_changed" || m.type === "agent_run") {
        loadAlerts();
      } else if (m.type === "event" && (m.event.type === "DISCHARGED" || m.event.type === "ADMITTED")) {
        window.setTimeout(census, 1500);
      }
    }, setUp);
    return () => { stop(); window.clearInterval(s); window.clearInterval(t); window.clearInterval(c); };
  }, [loadAlerts]);

  const patientMatch = route.match(/^#\/patients\/([\w-]+)/);
  return (
    <>
      <header className="topbar">
        <div className="brand"><h1><a href="#/" style={{ color: "inherit" }}>RPM Console</a></h1>
          <span className="muted small"><span className={`live-dot${up ? " on" : ""}`} /> {up ? "live" : "reconnecting"}</span>
        </div>
        <span className="muted small">Synthetic data · decision support, not a diagnosis</span>
        <div className="spacer" />
        {me.can.audit && <a href="#/audit" className="small">Audit trail</a>}
        <UserMenu me={me} />
        <div className="seg" role="group" aria-label="Theme">
          {(["system", "light", "dark"] as const).map((t) =>
            <button key={t} aria-pressed={theme === t} onClick={() => setTheme(t)}>{t}</button>)}
        </div>
      </header>
      {route.startsWith("#/audit") && me.can.audit ? <Audit />
        : patientMatch
        ? <PatientView id={patientMatch[1]} now={now} live={live} demo={me.can.demo && me.demo_controls !== false}
                       canAct={me.can.act} />
        : <Worklist patients={patients} alerts={alerts} stats={stats} trends={trends} flashed={flashed} now={now}
                    onChange={loadAlerts} canAct={me.can.act} />}
    </>
  );
}
