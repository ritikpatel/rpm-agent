// Sign-in for the console. On AWS: Amazon Cognito hosted UI, authorization code + PKCE (public client, no secret),
// refresh-token renewal. Locally: the API mints the same kind of token for a chosen role (RPM_ENV=local only).
// Tokens live in sessionStorage (per tab, cleared when the tab closes), never in localStorage or URLs.

export interface AuthConfig {
  mode: "dev" | "oidc";
  roles?: string[];
  client_id?: string; authorize_url?: string; token_url?: string; logout_url?: string; scope?: string;
}
export interface Me { username: string; role: string; roles: string[]; can: { read: boolean; act: boolean; demo: boolean; audit: boolean };
  demo_controls?: boolean }
interface Session { access_token: string; refresh_token?: string; expires_at: number }

const KEY = "rpm.session";
const PKCE = "rpm.pkce";
let config: AuthConfig | null = null;
let session: Session | null = null;
const listeners = new Set<() => void>();

try { session = JSON.parse(sessionStorage.getItem(KEY) ?? "null"); } catch { session = null; }

function save(s: Session | null) {
  session = s;
  try { s ? sessionStorage.setItem(KEY, JSON.stringify(s)) : sessionStorage.removeItem(KEY); } catch { /* ignore */ }
  listeners.forEach((l) => l());
}

export function onAuthChange(l: () => void): () => void { listeners.add(l); return () => listeners.delete(l); }
export const signedIn = () => session !== null;

export async function loadConfig(): Promise<AuthConfig> {
  if (!config) config = await (await fetch("/api/auth/config")).json() as AuthConfig;
  return config;
}

const redirectUri = () => `${location.origin}/callback`;

function b64url(bytes: ArrayBuffer | Uint8Array): string {
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  return btoa(String.fromCharCode(...arr)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
const random = (n: number) => b64url(crypto.getRandomValues(new Uint8Array(n)));

async function tokenRequest(params: Record<string, string>): Promise<Session> {
  const cfg = await loadConfig();
  const res = await fetch(cfg.token_url!, {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: cfg.client_id!, ...params }),
  });
  if (!res.ok) throw new Error(`sign-in failed (${res.status})`);
  const t = await res.json();
  return { access_token: t.access_token, refresh_token: t.refresh_token ?? session?.refresh_token,
           expires_at: Date.now() + t.expires_in * 1000 };
}

export async function signInDev(username: string, role: string): Promise<void> {
  const res = await fetch("/api/auth/dev/token", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ username, role }) });
  if (!res.ok) throw new Error(`sign-in failed (${res.status})`);
  const t = await res.json();
  save({ access_token: t.access_token, expires_at: Date.now() + t.expires_in * 1000 });
}

export async function signInOidc(): Promise<void> {
  const cfg = await loadConfig();
  const verifier = random(48);
  const state = random(16);
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  sessionStorage.setItem(PKCE, JSON.stringify({ verifier, state, back: location.hash }));
  location.assign(`${cfg.authorize_url}?${new URLSearchParams({
    response_type: "code", client_id: cfg.client_id!, redirect_uri: redirectUri(), scope: cfg.scope!,
    state, code_challenge: challenge, code_challenge_method: "S256" })}`);
}

/** On /callback: exchange the authorization code (state and PKCE verifier checked), then return to the app. */
export async function completeSignIn(): Promise<boolean> {
  if (location.pathname !== "/callback") return false;
  const q = new URLSearchParams(location.search);
  const pending = JSON.parse(sessionStorage.getItem(PKCE) ?? "null");
  sessionStorage.removeItem(PKCE);
  history.replaceState(null, "", `/${pending?.back ?? ""}`);
  if (!pending || q.get("state") !== pending.state || !q.get("code")) throw new Error("sign-in response rejected");
  save(await tokenRequest({ grant_type: "authorization_code", code: q.get("code")!, redirect_uri: redirectUri(),
                            code_verifier: pending.verifier }));
  return true;
}

let refreshing: Promise<void> | null = null;

/** A valid access token, refreshed shortly before expiry; null when the user has to sign in again. */
export async function accessToken(): Promise<string | null> {
  if (!session) return null;
  if (session.expires_at - Date.now() > 120_000) return session.access_token;
  if (!session.refresh_token) { save(null); return null; }
  refreshing ??= tokenRequest({ grant_type: "refresh_token", refresh_token: session.refresh_token })
    .then(save, () => save(null)).finally(() => { refreshing = null; });
  await refreshing;
  return session?.access_token ?? null;
}

export async function signOut(): Promise<void> {
  const cfg = await loadConfig();
  save(null);
  if (cfg.mode === "oidc") {
    location.assign(`${cfg.logout_url}?${new URLSearchParams({ client_id: cfg.client_id!, logout_uri: location.origin })}`);
  }
}

/** The API rejected the token (expired, revoked, or wrong role): drop the session so the sign-in screen shows. */
export function sessionRejected() { save(null); }
