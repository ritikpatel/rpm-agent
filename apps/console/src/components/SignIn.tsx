import { useState } from "react";
import { signInDev, signInOidc, type AuthConfig } from "../auth";

const DEV_ROLES: { role: string; username: string; label: string; note: string }[] = [
  { role: "physician+presenter+admin", username: "demo.lead", label: "Demo lead",
    note: "Everything: clinical actions, demo controls and the audit trail" },
  { role: "nurse", username: "nurse.jane", label: "RPM nurse", note: "Reads patients, acknowledges and acts" },
  { role: "physician", username: "dr.khan", label: "Physician", note: "Same clinical rights; tier-2 escalation" },
  { role: "admin", username: "ops.admin", label: "Admin / presenter", note: "Reads, demo controls, audit trail; no clinical actions" },
  { role: "presenter", username: "presenter", label: "Presenter only", note: "Demo controls only; no patient data" },
];

export default function SignIn({ config, error }: { config: AuthConfig; error?: string }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(error ?? "");
  const run = async (f: () => Promise<void>) => {
    setBusy(true); setMsg("");
    try { await f(); } catch (e) { setMsg(String(e instanceof Error ? e.message : e)); } finally { setBusy(false); }
  };
  return (
    <div className="page" style={{ maxWidth: 560, margin: "8vh auto" }}>
      <section className="card">
        <header><h2>RPM Console</h2><span className="muted small">Synthetic data · decision support</span></header>
        <div className="body stack">
          {config.mode === "oidc" ? <>
            <p className="small">Sign in with your organisation account. Access depends on your care-team role.</p>
            <button className="btn primary" disabled={busy} onClick={() => run(signInOidc)}>Sign in</button>
          </> : <>
            <p className="small muted">Local development sign-in: the API issues a token for the chosen role and
              enforces exactly the same checks as Cognito does on AWS.</p>
            {DEV_ROLES.filter((r) => !config.roles || config.roles.includes(r.role)).map((r) => (
              <button key={r.role} className="btn" disabled={busy} style={{ textAlign: "left", whiteSpace: "normal" }}
                      onClick={() => run(() => signInDev(r.username, r.role))}>
                <strong>{r.label}</strong> <span className="muted small">· {r.username} · {r.note}</span>
              </button>))}
          </>}
          {msg && <p className="small" role="alert">{msg}</p>}
        </div>
      </section>
    </div>
  );
}
