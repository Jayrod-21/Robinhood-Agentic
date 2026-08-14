// Client-side auth flows — the browser half of docs/AUTH_THREAT_MODEL.md §4.
//
// CSRF (§5.9): every state-changing call here sends `Content-Type:
// application/json`, the same contract lib/api.ts::postJSON carries — the
// app-wide guard (backend/app/main.py::enforce_same_origin) rejects anything
// else. postJSON is not reused verbatim for these POSTs because the auth
// contract needs two things it cannot express:
//   1. 204 success responses (login/totp, logout) carry no body, and postJSON's
//      unconditional res.json() rejects on an empty body.
//   2. A 423 lockout (§5.8) must surface `retry_after` honestly — which needs
//      the status code and the parsed error body, not a flattened Error string.
// authPost below is postJSON's exact header contract with those two behaviors
// added, kept in exactly one place so the CSRF-critical header cannot drift.
//
// Session state: __Host-rh_sid is HttpOnly. The client can never read it, and
// nothing in this file — or anywhere client-side — touches document.cookie.
// The only source of auth state is GET /api/auth/me (fetchMe below).
//
// Enumeration (§5.2): every password-step failure collapses into ONE `rejected`
// result. The server answers an identical shape for "unknown address" and
// "wrong password"; the client must not reconstruct a difference, so the result
// type here cannot even express one. Do not add finer-grained failure kinds.

import { API_URL, CREDENTIALS, getJSON } from "./api";
import type { LoginStepResponse, MeResponse, VerifyResponse } from "./types";

interface AuthHttpResponse {
  status: number;
  ok: boolean;
  /** Parsed JSON body; null when the response has none (204) or is not JSON. */
  body: unknown;
}

/** postJSON's header contract (§5.9), plus 204 tolerance and status exposure.
 *  Throws only on network failure — HTTP errors come back as data. */
async function authPost(path: string, payload: unknown): Promise<AuthHttpResponse> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    // Never optional and never a plain form: enforce_same_origin rejects any
    // state-changing request that is not application/json (§5.9).
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    // fetch's default, spelled out because it is load-bearing here: the login
    // 204's Set-Cookie is only stored — and the session cookie only replayed —
    // when the API shares the page's origin (lib/api.ts::CREDENTIALS explains
    // why "include" is NOT the alternative).
    credentials: CREDENTIALS,
  });
  let body: unknown = null;
  if (res.status !== 204) {
    body = await res.json().catch(() => null);
  }
  return { status: res.status, ok: res.ok, body };
}

/** Tolerates both `{retry_after}` and FastAPI-style `{detail: {retry_after}}`. */
function extractRetryAfter(body: unknown): number | null {
  if (body && typeof body === "object") {
    const o = body as Record<string, unknown>;
    if (typeof o.retry_after === "number") return o.retry_after;
    if (o.detail && typeof o.detail === "object") {
      const d = o.detail as Record<string, unknown>;
      if (typeof d.retry_after === "number") return d.retry_after;
    }
  }
  return null;
}

// --------------------------------------------------------------------------
// Step 1: password → challenge token (never a session)
// --------------------------------------------------------------------------

export type LoginResult =
  | { kind: "mfa_required"; challengeToken: string; expiresIn: number }
  /** ONE bucket for unknown address AND wrong password — §5.2, on purpose. */
  | { kind: "rejected" }
  | { kind: "locked"; retryAfterS: number }
  /** Network down or 5xx — honest and distinct from a rejection: nothing about
   *  the credentials was judged. */
  | { kind: "unavailable" };

export async function login(email: string, password: string): Promise<LoginResult> {
  let res: AuthHttpResponse;
  try {
    res = await authPost("/api/auth/login", { email, password });
  } catch {
    return { kind: "unavailable" };
  }
  if (res.status === 423) {
    return { kind: "locked", retryAfterS: extractRetryAfter(res.body) ?? 0 };
  }
  if (res.status >= 500) return { kind: "unavailable" };
  const body = res.body as LoginStepResponse | null;
  if (res.ok && body?.status === "mfa_required" && typeof body.challenge_token === "string") {
    return {
      kind: "mfa_required",
      challengeToken: body.challenge_token,
      // The contract sends expires_in; a missing value falls back to a short,
      // conservative window rather than pretending the challenge is durable.
      expiresIn: typeof body.expires_in === "number" ? body.expires_in : 120,
    };
  }
  // Everything else — identical-shape 200 failure, 4xx, malformed body — is a
  // single rejection. Whether the backend ships the failure as a 200 with a
  // different `status` or as a 4xx, this branch renders it identically.
  return { kind: "rejected" };
}

// --------------------------------------------------------------------------
// Step 2: challenge + code (TOTP digits OR a recovery code, same field) → session
// --------------------------------------------------------------------------

export type TotpResult =
  | { kind: "authenticated" }
  /** Wrong/replayed code or a dead challenge — the server does not say which. */
  | { kind: "rejected" }
  | { kind: "locked"; retryAfterS: number }
  | { kind: "unavailable" };

export async function submitTotp(challengeToken: string, code: string): Promise<TotpResult> {
  let res: AuthHttpResponse;
  try {
    res = await authPost("/api/auth/login/totp", { challenge_token: challengeToken, code });
  } catch {
    return { kind: "unavailable" };
  }
  // 204 + Set-Cookie: __Host-rh_sid — the browser stores the cookie itself;
  // there is nothing for JS to (or that JS can) do with it.
  if (res.status === 204) return { kind: "authenticated" };
  if (res.status === 423) {
    return { kind: "locked", retryAfterS: extractRetryAfter(res.body) ?? 0 };
  }
  if (res.status >= 500) return { kind: "unavailable" };
  return { kind: "rejected" };
}

// --------------------------------------------------------------------------
// Logout
// --------------------------------------------------------------------------

/** Logout is the server stamping `revoked_at` on the session row (§5.3);
 *  clearing the cookie alone is not a logout, and the HttpOnly cookie is out of
 *  reach here anyway. Returns false when the server did NOT confirm — in that
 *  case the session is still live and the UI must not pretend otherwise. */
export async function logout(): Promise<boolean> {
  try {
    const res = await authPost("/api/auth/logout", {});
    return res.ok;
  } catch {
    return false;
  }
}

// --------------------------------------------------------------------------
// Email verification (§5.6)
// --------------------------------------------------------------------------

export type VerifyResult =
  | { kind: "verified" }
  | { kind: "already_verified" }
  /** Expired TTL, superseded by a newer link, already consumed, malformed, or
   *  bound to an address the operator has since changed. `reason` passes the
   *  server's stated status through when it gives one — this page sits AFTER
   *  the email round-trip, so §5.2's enumeration constraint does not apply and
   *  being specific about why a link failed is helpful, not dangerous. */
  | { kind: "rejected"; reason: string | null }
  | { kind: "unavailable" };

/** Pulls a machine-readable status out of an error body, tolerating both
 *  `{status}` and FastAPI-style `{detail}` / `{detail: {status}}` layouts. */
function extractStatusReason(body: unknown): string | null {
  if (body && typeof body === "object") {
    const o = body as Record<string, unknown>;
    if (typeof o.status === "string") return o.status;
    if (typeof o.detail === "string") return o.detail;
    if (o.detail && typeof o.detail === "object") {
      const d = o.detail as Record<string, unknown>;
      if (typeof d.status === "string") return d.status;
    }
  }
  return null;
}

export async function verifyEmail(token: string): Promise<VerifyResult> {
  let res: AuthHttpResponse;
  try {
    res = await authPost("/api/auth/verify", { token });
  } catch {
    return { kind: "unavailable" };
  }
  if (res.status >= 500) return { kind: "unavailable" };
  const body = res.body as VerifyResponse | null;
  if (res.ok) {
    return { kind: body?.status === "already_verified" ? "already_verified" : "verified" };
  }
  // Losing the §5.6 double-consume race resolves to a friendly already_verified
  // even if the backend chooses to ship it on a non-2xx status.
  const reason = extractStatusReason(res.body);
  if (reason === "already_verified") return { kind: "already_verified" };
  return { kind: "rejected", reason };
}

/** §5.2: the server answers an identical 202 for every state — unknown address,
 *  already verified, cooldown-suppressed. The client mirrors that honesty: the
 *  only outcomes are "accepted" and "unavailable", never a hint about whether
 *  the address exists or whether a mail was actually sent. */
export type ResendResult = "accepted" | "unavailable";

export async function resendVerification(email: string): Promise<ResendResult> {
  try {
    const res = await authPost("/api/auth/verify/resend", { email });
    return res.ok ? "accepted" : "unavailable";
  } catch {
    return "unavailable";
  }
}

// --------------------------------------------------------------------------
// Auth state
// --------------------------------------------------------------------------

/** Resolves null when logged out, when the session has expired or been revoked,
 *  or when the auth backend is unreachable/not yet built — callers render the
 *  logged-out state in all of those cases rather than try/catching. */
export async function fetchMe(): Promise<MeResponse | null> {
  try {
    return await getJSON<MeResponse>("/api/auth/me");
  } catch {
    return null;
  }
}
