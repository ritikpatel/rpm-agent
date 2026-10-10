// Audit trail: who read or changed which patient's data. Clinicians, services and each agent appear by name.
import { useEffect, useState } from "react";
import { api, type AuditRow } from "../api";

export default function Audit() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string>();
  const load = () => api.audit(300).then(setRows).catch((e) => setError(String(e)));
  useEffect(() => { void load(); const t = window.setInterval(load, 10000); return () => window.clearInterval(t); }, []);
  const shown = rows.filter((r) => !filter
    || `${r.actor} ${r.action} ${r.resource_type} ${r.patient_id ?? ""}`.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 12, justifyContent: "space-between" }}>
        <div className="row"><a href="#/">← Worklist</a><h1 style={{ marginLeft: 8 }}>Audit trail</h1>
          <span className="muted">latest {rows.length} entries · append-only · exported hourly as FHIR AuditEvent</span></div>
        <input type="search" placeholder="Filter by actor, action, patient" value={filter}
               onChange={(e) => setFilter(e.target.value)} aria-label="Filter audit entries" />
      </div>
      {error && <div className="muted">{error}</div>}
      <section className="card"><div className="scroll" style={{ maxHeight: "75vh" }}>
        <table className="small">
          <thead><tr><th>Time</th><th>Who</th><th>Role</th><th>Action</th><th>Resource</th><th>Patient</th>
            <th>Outcome</th></tr></thead>
          <tbody>{shown.map((r) => (
            <tr key={r.id}><td>{new Date(r.at).toLocaleTimeString()}</td><td>{r.actor}</td>
              <td>{r.actor_role ?? "–"}</td><td>{r.action}</td>
              <td>{r.resource_type}{r.resource_id ? ` · ${r.resource_id.slice(0, 18)}` : ""}</td>
              <td>{r.patient_id ? <a href={`#/patients/${r.patient_id}`}>{r.patient_id}</a> : "–"}</td>
              <td>{r.outcome}</td></tr>))}
          </tbody>
        </table>
      </div></section>
    </div>
  );
}
