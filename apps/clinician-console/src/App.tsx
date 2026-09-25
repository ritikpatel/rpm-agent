import { useCallback, useEffect, useRef, useState } from "react";
import { api, connectLive, getUser, setUser, USERS, type AlertRow, type LiveMessage, type PatientRow,
         type Stats } from "./api";
import PatientView from "./pages/PatientView";
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

export default function App() {
  const route = useHashRoute();
  const [patients, setPatients] = useState<PatientRow[]>([]);
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [trends, setTrends] = useState<Record<string, number[]>>({});
  const [flashed, setFlashed] = useState<Set<string>>(new Set());
  const [live, setLive] = useState<LiveMessage | null>(null);
  const [up, setUp] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [user, setUserState] = useState(getUser().id);
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
    api.patients().then(setPatients).catch(() => {});
    api.alerts().then(setAlerts).catch(() => {});
    const refresh = () => api.stats().then(setStats).catch(() => {});
    void refresh();
    const s = window.setInterval(refresh, 5000);
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
      }
    }, setUp);
    return () => { stop(); window.clearInterval(s); window.clearInterval(t); };
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
        <label className="small muted">Signed in as{" "}
          <select value={user} onChange={(e) => { setUser(e.target.value); setUserState(e.target.value); }}>
            {USERS.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
          </select>
        </label>
        <div className="seg" role="group" aria-label="Theme">
          {(["system", "light", "dark"] as const).map((t) =>
            <button key={t} aria-pressed={theme === t} onClick={() => setTheme(t)}>{t}</button>)}
        </div>
      </header>
      {patientMatch
        ? <PatientView id={patientMatch[1]} now={now} live={live} demo />
        : <Worklist patients={patients} alerts={alerts} stats={stats} trends={trends} flashed={flashed} now={now}
                    onChange={loadAlerts} />}
    </>
  );
}
