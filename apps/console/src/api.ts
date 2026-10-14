import { accessToken, sessionRejected, type Me } from "./auth";

// Typed client for care-api and its live WebSocket feed.

export type Latest = Record<string, { v: number | string; ts: string }>;

export interface PatientRow {
  patient_id: string; display_name: string; sex: string; age: number; primary_condition: string;
  primary_diagnosis: string | null; data_origin: string | null;
  care_setting: "home" | "icu"; discharge_at: string | null; device_id: string | null; last_seen: string | null;
  news2: number | null; news2_band: string | null; news2_partial: boolean | null; latest: Latest | null;
  alerts_24h: number;
  risk: Record<string, { probability: number; band: string; badge?: string | null; tone?: string | null }> | null;
}

export interface VitalRow {
  ts: string; heart_rate: number | null; spo2: number | null; resp_rate: number | null; sbp: number | null;
  dbp: number | null; temp: number | null; gcs: number | null; hrv_rmssd: number | null; skin_temp: number | null;
  news2: number | null; news2_band: string | null; news2_partial: boolean | null; buffered: boolean; late: boolean;
  activity: string | null; source: string;
}

export interface AlertRow {
  alert_id: string; patient_id: string; status: string; severity: "medium" | "high" | "critical"; tier: number;
  title: string; event_types: string[]; event_count: number; opened_at: string; updated_at: string;
  acknowledged_at: string | null; acknowledged_by: string | null; brief_status: string; brief_headline: string | null;
  next_escalation_at: string | null; display_name: string; care_setting: string; primary_condition: string;
}

export interface Brief {
  headline: string; situation: string; background: string; assessment: string; recommendation: string;
  citations: { ref: string; supports: string }[]; uncertainties: string[]; generated_by?: string;
  citation_check?: string;
}

export interface TimelineEntry { at: string; kind: string; actor: string; summary: string; details: unknown }
export interface NotificationRow {
  tier: number; channel: string; recipient: string; status: string; sent_at: string; latency_ms: number | null; cause: string | null;
  error: string | null;
}

export interface AlertDetail extends AlertRow {
  brief: Brief | null; agent_assessment: Assessment | null; timeline: TimelineEntry[];
  notifications: NotificationRow[]; first_event_at: string; resolution: string | null;
  agent_runs: { run_id: string; status: string; provider: string; model: string | null; started_at: string;
                input_tokens: number | null; output_tokens: number | null; error: string | null }[];
}

export interface Assessment {
  signal_quality: string; concern_level: string; clinical_picture: string;
  key_findings: { finding: string; evidence: string }[];
  recommended_actions: { action: string; reason: string }[]; model_evidence_note: string; confidence: string;
}

/** A model's own reading of its result (features.py `explain`; see models/CONTRACT.md). */
export interface RiskOutput {
  headline: { label: string; value: number | string | null; suffix?: string; status: string; tone: string; trend?: string };
  badge?: string; details?: string[]; concerns?: string[]; reassurances?: string[]; note?: string;
}

export interface RiskRow {
  model: string; version: string; title: string | null; outcome: string | null; owner: string | null;
  label: string | null; short_label: string | null; output: RiskOutput | null;
  probability: number; band: string; computed_at: string;
  top_factors: { feature: string; value: number; contribution: number }[]; validated: boolean | null;
  metrics: Record<string, number | string> | null;
}

export interface MessageRow { at: string; direction: "to_patient" | "from_patient"; kind: string; text: string; sender: string }
export interface LabRow { ts: string; code: string; name: string; value: number | null; unit: string | null; flag: string | null }
export interface AgentRun {
  run_id: string; alert_id: string | null; trigger: string; status: string; provider: string | null;
  model: string | null; started_at: string; finished_at: string | null; assessment: Assessment | null;
  actions: { action: string; reason: string; status: string }[]; brief: Brief | null;
  input_tokens: number | null; output_tokens: number | null; error: string | null;
}
export interface AuditRow {
  id: number; at: string; actor: string; actor_role: string | null; action: string; resource_type: string;
  resource_id: string | null; patient_id: string | null; outcome: string;
}

export interface EventRow {
  event_id: string; patient_id: string; type: string; severity: string; requires_alert: boolean;
  detected_at: string; summary: string;
}

export interface Stats {
  patients: number; icu: number; home: number; streaming: number; band_high: number; band_medium: number;
  band_low_medium: number; windows_5m: number; e2e_p50_s: number | null; e2e_p95_s: number | null;
  pages_1h: number; page_p95_s: number | null; events_1h: Record<string, number>;
}

export interface Chunk { chunk_id: string; title: string; heading: string; text: string; kind: string }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await accessToken();
  if (!token) { sessionRejected(); throw new Error("not signed in"); }
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", authorization: `Bearer ${token}`, ...(init?.headers ?? {}) },
  });
  if (res.status === 401) sessionRejected();  // expired or revoked: back to sign-in (403 = role, not session)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

const post = <T,>(path: string, body: unknown) => call<T>(path, { method: "POST", body: JSON.stringify(body) });

export const api = {
  me: () => call<Me>("/api/auth/me"),
  patients: () => call<PatientRow[]>("/api/patients"),
  patient: (id: string) => call<PatientRow & Record<string, unknown>>(`/api/patients/${id}`),
  vitals: (id: string, minutes: number) => call<VitalRow[]>(`/api/patients/${id}/vitals?minutes=${minutes}&limit=5000`),
  events: (limit = 50, patientId?: string) =>
    call<EventRow[]>(`/api/events?limit=${limit}${patientId ? `&patient_id=${patientId}` : ""}`),
  stats: () => call<Stats>("/api/stats"),
  alerts: (status = "active") => call<AlertRow[]>(`/api/alerts?status=${status}`),
  alert: (id: string) => call<AlertDetail>(`/api/alerts/${id}`),
  ack: (id: string, note?: string) => post(`/api/alerts/${id}/ack`, { note }),
  escalate: (id: string, reason: string) => post(`/api/alerts/${id}/escalate`, { reason }),
  resolve: (id: string, resolution: string) => post(`/api/alerts/${id}/resolve`, { resolution }),
  risk: (id: string) => call<RiskRow[]>(`/api/patients/${id}/risk`),
  summary: (id: string) => call<{ title: string; content: string }>(`/api/patients/${id}/summary`),
  chunk: (id: string) => call<Chunk>(`/api/knowledge/${encodeURIComponent(id)}`),
  command: (id: string, body: { cmd: string; topic?: string; profile?: string }) =>
    post<{ cmd_id: string }>(`/api/patients/${id}/commands`, body),
  scenario: (body: { patient_id: string; scenario: string; ramp_s?: number }) => post("/api/demo/scenario", body),
  clearScenario: (patient_id: string) => post("/api/demo/clear", { patient_id, scenario: "clear" }),
  discharge: (patient_id: string) => post("/api/demo/discharge", { patient_id }),
  admit: (patient_id: string) => post("/api/demo/admit", { patient_id }),
  lab: (body: { patient_id: string; name: string; value: number; unit?: string; flag?: string }) =>
    post("/api/demo/lab", body),
  labs: (id: string) => call<LabRow[]>(`/api/patients/${id}/labs`),
  messages: (id: string) => call<MessageRow[]>(`/api/patients/${id}/messages`),
  sendMessage: (id: string, text: string) => post(`/api/patients/${id}/messages`, { text }),
  agentRuns: (id: string) => call<AgentRun[]>(`/api/patients/${id}/agent-runs?limit=10`),
  agentReview: (id: string) => post<{ queued: boolean }>(`/api/patients/${id}/agent-review`, {}),
  assignDevice: (id: string, device_id: string) => post(`/api/patients/${id}/device`, { device_id }),
  audit: (limit = 200) => call<AuditRow[]>(`/api/audit?limit=${limit}`),
};

export type LiveMessage =
  | { type: "vitals"; patient_id: string; device_id: string; ts: string; vitals: Record<string, number>;
      news2: { total: number; band: string; partial: boolean } | null; late: boolean; buffered: boolean }
  | { type: "event"; event: EventRow }
  | { type: "alert"; alert: AlertRow | null }
  | { type: "alert_changed"; alert_id: string }
  | { type: "agent_run"; run_id: string; patient_id: string; alert_id: string | null; status: string }
  | { type: "message"; patient_id: string }
  | { type: "risk"; patient_id: string; scores: { model: string; probability: number; band: string }[] };

export function connectLive(onMessage: (m: LiveMessage) => void, onStatus: (up: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry: number | undefined;
  const open = async () => {
    const token = await accessToken();  // fresh (refreshed if needed) on every reconnect
    if (closed) return;
    if (!token) { sessionRejected(); return; }
    // the token rides as a WebSocket subprotocol: browsers cannot set Authorization, and URLs end up in logs
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`, ["rpm.v1", token]);
    ws.onopen = () => onStatus(true);
    ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch { /* ignore malformed */ } };
    ws.onclose = () => { onStatus(false); if (!closed) retry = window.setTimeout(() => { void open(); }, 2000); };
  };
  void open();
  return () => { closed = true; window.clearTimeout(retry); ws?.close(); };
}
