"use client";

// Operator sign-in — the client side of docs/AUTH_THREAT_MODEL.md §4.
//
// Login is TWO steps and the password step never yields a session: it mints a
// short-lived challenge token whose only power is the right to attempt the
// second-factor step. The session cookie (__Host-rh_sid, HttpOnly) is set by
// the server on the 204 from /api/auth/login/totp; nothing on this page reads
// or writes cookies, ever.
//
// §5.2 (enumeration): an unknown address and a wrong password surface as the
// SAME message from the SAME code path — lib/auth.ts collapses every
// password-step failure into one `rejected` result, so this page cannot even
// express a "no such account" state. Do not add one.
//
// §5.8 (lockout): a 423 is shown honestly — the remaining wait, the reason,
// and the host-CLI override — never as a generic failure. The form is NOT
// disabled while locked: the lock is per-operator server-side, and blocking
// the other (valid) operator client-side is exactly the silently-blocking-
// guardrail failure this project already paid for.

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertTriangle, KeyRound, ShieldCheck, TimerReset } from "lucide-react";
import { Button, Card, CardBody, Spinner } from "@/components/ui";
import { cn } from "@/lib/format";
import { fetchMe, login, submitTotp } from "@/lib/auth";

/** The single message for every password-step rejection (§5.2). One constant,
 *  one branch — unknown address and wrong password are indistinguishable. */
const REJECTED_MSG = "Sign-in failed — check the address and password.";

/** The server's cap is 256 BYTES of UTF-8 (§5.1: it bounds Argon2's cost;
 *  services/auth.py::_MAX_PASSWORD_BYTES, and bin/manage_operator.py enforces
 *  the same cap at seeding, so no valid passphrase can exceed it). The old
 *  `maxLength={256}` counted UTF-16 code units instead: it silently truncated
 *  long ASCII passphrases mid-typing, while a long non-ASCII passphrase slipped
 *  through under 256 characters but over 256 bytes and hit the server's
 *  dummy-verify path — indistinguishable from "wrong password", with no way for
 *  the operator to learn why. So the input is uncapped and the check is done
 *  here, in the server's own byte semantics, with an honest client-side message
 *  BEFORE anything is sent. This leaks nothing (§5.2): it judges only the
 *  operator's own keystrokes against a publicly documented policy, and no
 *  request is made. Raising the server cap instead was rejected — the cap is a
 *  deliberate Argon2 cost bound, and the seeder guarantees it cannot exclude a
 *  real passphrase. */
const PASSWORD_MAX_BYTES = 256;
const passwordBytes = (p: string) => new TextEncoder().encode(p).length;
const OVERSIZE_MSG =
  `That passphrase is over the ${PASSWORD_MAX_BYTES}-byte limit (bytes, not characters — ` +
  "accented and non-Latin characters count as several). Operator passphrases are seeded under " +
  "the same limit, so this one cannot be right as typed. Nothing was sent.";

const inputCls =
  "w-full rounded-lg border border-ink-700 bg-ink-950/60 px-3.5 py-2.5 text-sm text-zinc-100 " +
  "placeholder:text-zinc-600 outline-none transition-colors focus:border-brass/60 focus:bg-ink-950";

type Phase =
  | { step: "password" }
  | { step: "totp"; challengeToken: string; expiresAt: number };

function fmtWait(totalS: number): string {
  const s = Math.max(0, totalS);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}m ${r.toString().padStart(2, "0")}s` : `${r}s`;
}

/** Where to land after a successful sign-in, from the `next` query parameter.
 *
 *  `next` arrives in a URL, so it is attacker-supplied: a link to
 *  /login?next=https://evil.example would otherwise turn this page into an open
 *  redirect that borrows the dashboard's hostname to make a phishing landing
 *  look legitimate — and it would fire immediately after a real sign-in, when
 *  the operator has every reason to trust what they see.
 *
 *  Accepted only if it is a path on this origin: one leading slash, and NOT a
 *  second (`//evil.example` is protocol-relative — the browser reads it as a
 *  different HOST, not a path). Everything else falls back to the dashboard. */
function safeNextPath(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return "/";
  return raw;
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const nextPath = safeNextPath(searchParams.get("next"));
  const [phase, setPhase] = useState<Phase>({ step: "password" });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [lockedUntil, setLockedUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  // Already signed in? (Auth state comes from /api/auth/me — the cookie is
  // HttpOnly and unreadable.) fetchMe resolves null while the backend doesn't
  // exist yet, so this is a no-op until auth ships.
  useEffect(() => {
    let cancelled = false;
    void fetchMe().then((me) => {
      if (me && !cancelled) router.replace(nextPath);
    });
    return () => {
      cancelled = true;
    };
  }, [router, nextPath]);

  // 1 s ticker, only while something on screen counts down.
  const ticking = phase.step === "totp" || lockedUntil !== null;
  useEffect(() => {
    if (!ticking) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [ticking]);

  // Challenge TTL elapsed → back to the password step, honestly.
  useEffect(() => {
    if (phase.step === "totp" && now >= phase.expiresAt) {
      setPhase({ step: "password" });
      setCode("");
      setError(null);
      setNotice("That sign-in attempt expired. Enter your password again.");
    }
  }, [now, phase]);

  // Lockout window elapsed → clear the banner.
  useEffect(() => {
    if (lockedUntil !== null && now >= lockedUntil) setLockedUntil(null);
  }, [now, lockedUntil]);

  const lockedForS = lockedUntil !== null ? Math.ceil((lockedUntil - now) / 1000) : 0;
  const challengeLeftS = phase.step === "totp" ? Math.ceil((phase.expiresAt - now) / 1000) : 0;

  async function onPasswordSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    // Byte-cap check (see PASSWORD_MAX_BYTES): fail honestly here rather than
    // let the server's dummy-verify path report an unexplainable rejection.
    if (passwordBytes(password) > PASSWORD_MAX_BYTES) {
      setNotice(null);
      setError(OVERSIZE_MSG);
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    const r = await login(email.trim(), password);
    setBusy(false);
    if (r.kind === "mfa_required") {
      setPassword(""); // challenge in hand — drop the password from state
      setPhase({
        step: "totp",
        challengeToken: r.challengeToken,
        expiresAt: Date.now() + r.expiresIn * 1000,
      });
    } else if (r.kind === "locked") {
      setLockedUntil(Date.now() + r.retryAfterS * 1000);
    } else if (r.kind === "unavailable") {
      setError("The server could not be reached. Nothing was checked — try again.");
    } else {
      // ONE branch for every rejection — §5.2.
      setError(REJECTED_MSG);
    }
  }

  async function onTotpSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || phase.step !== "totp") return;
    setBusy(true);
    setError(null);
    setNotice(null);
    const r = await submitTotp(phase.challengeToken, code.trim());
    setBusy(false);
    if (r.kind === "authenticated") {
      // Full navigation, not router.push: the shell must refetch /api/auth/me
      // with the fresh session cookie attached.
      window.location.assign(nextPath);
    } else if (r.kind === "locked") {
      setLockedUntil(Date.now() + r.retryAfterS * 1000);
      setCode("");
    } else if (r.kind === "unavailable") {
      setError("The server could not be reached. Try again.");
    } else {
      setError("That code was not accepted. Codes are single-use and short-lived — take a fresh one.");
      setCode("");
    }
  }

  function backToPassword() {
    setPhase({ step: "password" });
    setCode("");
    setError(null);
    setNotice(null);
  }

  const feedback = (
    <>
      {error && (
        <div role="alert" className="flex items-start gap-2 text-sm text-loss">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {notice && !error && <div className="text-sm text-flat">{notice}</div>}
    </>
  );

  return (
    <div className="flex min-h-[80vh] items-center justify-center">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="font-serif text-3xl text-zinc-100">Agentic</div>
          <div className="mt-1.5 text-[11px] uppercase tracking-[0.3em] text-brass">Operator sign-in</div>
        </div>

        {lockedUntil !== null && (
          <Card className="mb-4 border-flat/40 bg-flat/5">
            <CardBody className="pt-4">
              <div className="flex items-center gap-2 text-sm font-medium text-flat">
                <TimerReset className="h-4 w-4" />
                Account locked — <span className="tnum">{fmtWait(lockedForS)}</span> remaining
              </div>
              <p className="mt-1.5 text-xs leading-relaxed text-zinc-400">
                Five failed codes lock the account for a fixed window. This is the lockout doing its
                job, not an outage — it clears on its own, or immediately from the host:{" "}
                <code className="text-zinc-300">bin/manage_operator.py unlock</code>. The other
                operator&rsquo;s account is unaffected.
              </p>
            </CardBody>
          </Card>
        )}

        <Card>
          <CardBody className="pt-5">
            <div className="mb-5 flex items-center justify-between">
              <span className="text-xs uppercase tracking-widest text-zinc-500">
                {phase.step === "password" ? "Step 1 · Password" : "Step 2 · Second factor"}
              </span>
              <span className="flex gap-1" aria-hidden>
                <span className="h-1.5 w-6 rounded-full bg-brass" />
                <span className={cn("h-1.5 w-6 rounded-full", phase.step === "totp" ? "bg-brass" : "bg-ink-700")} />
              </span>
            </div>

            {phase.step === "password" ? (
              <form onSubmit={onPasswordSubmit} className="space-y-4">
                <div>
                  <label htmlFor="email" className="mb-1.5 block text-xs uppercase tracking-widest text-zinc-500">
                    Email
                  </label>
                  <input
                    id="email"
                    type="email"
                    required
                    autoFocus
                    autoComplete="username"
                    spellCheck={false}
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className={inputCls}
                    placeholder="operator@…"
                  />
                </div>
                <div>
                  <label htmlFor="password" className="mb-1.5 block text-xs uppercase tracking-widest text-zinc-500">
                    Password
                  </label>
                  <input
                    id="password"
                    type="password"
                    required
                    autoComplete="current-password"
                    // No maxLength: it counts UTF-16 code units, not the server's
                    // bytes, and silently truncating a secret is worse than
                    // rejecting it — onPasswordSubmit enforces the real
                    // PASSWORD_MAX_BYTES cap with an explanation instead.
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className={inputCls}
                    placeholder="••••••••••••"
                  />
                </div>
                {feedback}
                <Button type="submit" variant="brass" disabled={busy} className="w-full">
                  {busy ? (
                    <>
                      <Spinner className="border-ink-950/30 border-t-ink-950" /> Checking…
                    </>
                  ) : (
                    <>
                      <KeyRound className="h-4 w-4" /> Continue
                    </>
                  )}
                </Button>
              </form>
            ) : (
              <form onSubmit={onTotpSubmit} className="space-y-4">
                <p className="text-sm text-zinc-400">
                  Signing in as <span className="text-zinc-200">{email}</span>
                </p>
                <div>
                  <label htmlFor="code" className="mb-1.5 block text-xs uppercase tracking-widest text-zinc-500">
                    Authenticator code
                  </label>
                  <input
                    id="code"
                    required
                    autoFocus
                    autoComplete="one-time-code"
                    spellCheck={false}
                    maxLength={24}
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    className={cn(inputCls, "font-mono tracking-[0.2em]")}
                    placeholder="123456"
                  />
                  <p className="mt-1.5 text-[11px] leading-relaxed text-zinc-600">
                    The 6-digit code from your authenticator — or a 10-character recovery code if the
                    device is gone. Same field, either works. Using a recovery code signs out every
                    other session.
                  </p>
                </div>
                {feedback}
                <Button type="submit" variant="brass" disabled={busy} className="w-full">
                  {busy ? (
                    <>
                      <Spinner className="border-ink-950/30 border-t-ink-950" /> Verifying…
                    </>
                  ) : (
                    <>
                      <ShieldCheck className="h-4 w-4" /> Verify
                    </>
                  )}
                </Button>
                <div className="flex items-center justify-between text-[11px] text-zinc-600">
                  <button
                    type="button"
                    onClick={backToPassword}
                    className="text-zinc-500 transition-colors hover:text-zinc-200"
                  >
                    ← Different account
                  </button>
                  <span className="tnum">challenge expires in {fmtWait(challengeLeftS)}</span>
                </div>
              </form>
            )}
          </CardBody>
        </Card>

        <p className="mt-5 text-center text-[11px] leading-relaxed text-zinc-600">
          Two seeded operator accounts — no signup, no emailed password reset. Locked out for real?
          Recovery runs on the host: <code>bin/manage_operator.py</code>.
        </p>
      </div>
    </div>
  );
}

/** useSearchParams() forces a client-side bail-out, which Next requires to sit
 *  behind a Suspense boundary or the /login prerender fails outright (it did).
 *  The fallback is the page's own frame, so a signed-out visitor never sees a
 *  blank screen while the client bundle hydrates — the failure mode that sent
 *  this page's predecessor to a grey page with no way forward. */
export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center px-4">
          <Spinner />
        </div>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
