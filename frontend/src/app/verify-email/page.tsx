"use client";

// Email-verification landing — docs/AUTH_THREAT_MODEL.md §5.6.
//
// The route is /verify-email because that is the link the email service builds
// (backend/app/services/email.py:261, format pinned by its tests):
//     {origin}/verify-email#token=<urlencoded token>
//
// The token rides in the URL FRAGMENT, deliberately (9b fix-pass SF-2, ported):
// fragments are never transmitted to the server, so the token cannot appear in
// a Referer header, a Cloudflare edge log, or an origin access log. The
// consequence: THE SERVER NEVER SEES THE TOKEN — this must be a client
// component that reads location.hash, scrubs it, and POSTs the token as a JSON
// body to /api/auth/verify. The token is never logged, never sent anywhere
// else, and never placed in an element attribute.
//
// Intended, not a bug (§5.9): SameSite=Strict suppresses the session cookie on
// the cross-site navigation from a mail client, so this page always arrives
// logged-out. /api/auth/verify is an unauthenticated route by design, and
// redeeming a token confers NO session (§5.6) — it stamps email_verified_at
// and nothing else. The visitor signs in as usual afterwards.
//
// This page sits after the email round-trip, so §5.2's enumeration constraint
// does NOT apply here: each failure gets a distinct, honest message.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, MailCheck, MailQuestion, Send, TimerOff } from "lucide-react";
import { Button, Card, CardBody, Spinner } from "@/components/ui";
import { resendVerification, verifyEmail } from "@/lib/auth";

const inputCls =
  "w-full rounded-lg border border-ink-700 bg-ink-950/60 px-3.5 py-2.5 text-sm text-zinc-100 " +
  "placeholder:text-zinc-600 outline-none transition-colors focus:border-brass/60 focus:bg-ink-950";

type View =
  | "reading" // pre-hydration / first paint
  | "no_token" // fragment absent entirely
  | "malformed" // fragment present but the token doesn't survive the shape gate
  | "verifying"
  | "verified"
  | "already_verified"
  | "expired" // server said the TTL elapsed
  | "consumed" // server said the token was already redeemed
  | "rejected" // server refused for another reason (superseded, stale address, …)
  | "unavailable";

/** Map the server's stated reason (auth.ts passes it through) onto a view.
 *  Unrecognized reasons fall to the generic rejection. */
function viewForReason(reason: string | null): View {
  if (reason) {
    const r = reason.toLowerCase();
    if (r.includes("expire")) return "expired";
    if (r.includes("consum") || r.includes("used")) return "consumed";
  }
  return "rejected";
}

export default function VerifyEmailPage() {
  const [view, setView] = useState<View>("reading");
  // Kept so "Try again" after a server-unreachable result can re-POST the same
  // token — the fragment was scrubbed and cannot be re-read.
  const tokenRef = useRef<string | null>(null);
  const started = useRef(false);

  async function run(token: string) {
    setView("verifying");
    const r = await verifyEmail(token);
    setView(r.kind === "rejected" ? viewForReason(r.reason) : r.kind);
  }

  useEffect(() => {
    // React StrictMode re-runs effects in dev; the hash is consumed on the
    // first pass, so a second pass must not clobber the state with "no_token".
    if (started.current) return;
    started.current = true;
    // The link is built with quote(token, safe='') — URLSearchParams both
    // splits the k=v pair and percent-DEcodes the value in one step.
    const hash = window.location.hash.replace(/^#/, "");
    const token = hash ? new URLSearchParams(hash).get("token") : null;
    if (token !== null) {
      // Scrub the fragment immediately, before any await, so the token doesn't
      // linger in the address bar, the history entry, or a copied URL. (It was
      // never on the wire — fragments don't transmit — this closes the
      // over-the-shoulder / copy-paste tail.)
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    if (token === null) {
      setView("no_token");
      return;
    }
    // Same shape gate the server applies (§5.6): 32 CSPRNG bytes base64url is
    // 43 chars from [A-Za-z0-9_-]. A token that fails this was mangled in
    // transit (mail-client truncation, line wrap) — say so, don't POST noise.
    if (!/^[A-Za-z0-9_-]{16,}$/.test(token)) {
      setView("malformed");
      return;
    }
    tokenRef.current = token;
    void run(token);
  }, []);

  return (
    <div className="flex min-h-[80vh] items-center justify-center">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="font-serif text-3xl text-zinc-100">Agentic</div>
          <div className="mt-1.5 text-[11px] uppercase tracking-[0.3em] text-brass">Email verification</div>
        </div>

        <Card>
          <CardBody className="pt-5">
            {(view === "reading" || view === "verifying") && (
              <div className="flex items-center gap-3 py-2 text-sm text-zinc-400">
                <Spinner /> Confirming your address…
              </div>
            )}

            {view === "verified" && (
              <Outcome
                icon={<MailCheck className="h-5 w-5 text-gain" />}
                title="Address verified"
                titleClass="text-gain"
                body={
                  <>
                    The alerting channel reaches you — that is all this proves. The link deliberately
                    carried no sign-in powers, and the session cookie is absent on arrival from a
                    mail client by design. Sign in as usual.
                  </>
                }
                action={<SignInLink />}
              />
            )}

            {view === "already_verified" && (
              <Outcome
                icon={<MailCheck className="h-5 w-5 text-zinc-300" />}
                title="Already verified"
                titleClass="text-zinc-200"
                body={<>This address was confirmed earlier — nothing more to do here.</>}
                action={<SignInLink />}
              />
            )}

            {view === "expired" && (
              <Outcome
                icon={<TimerOff className="h-5 w-5 text-flat" />}
                title="This link has expired"
                titleClass="text-flat"
                body={
                  <>
                    Verification links live for a fixed window and this one&rsquo;s has elapsed.
                    Request a fresh link below — it supersedes every earlier one.
                  </>
                }
                action={<ResendBlock />}
              />
            )}

            {view === "consumed" && (
              <Outcome
                icon={<MailCheck className="h-5 w-5 text-zinc-300" />}
                title="This link was already used"
                titleClass="text-zinc-200"
                body={
                  <>
                    Each link is single-use and this one has been redeemed. If that was you, the
                    address is verified — just sign in. If it wasn&rsquo;t, request a fresh link
                    and mention it to the other operator.
                  </>
                }
                action={
                  <div className="space-y-4">
                    <SignInLink />
                    <ResendBlock />
                  </div>
                }
              />
            )}

            {view === "rejected" && (
              <Outcome
                icon={<AlertTriangle className="h-5 w-5 text-loss" />}
                title="This link is no longer valid"
                titleClass="text-loss"
                body={
                  <>
                    Every newly issued link supersedes the ones before it, and a link dies if the
                    account&rsquo;s address changed after it was sent. Request a fresh one below.
                  </>
                }
                action={<ResendBlock />}
              />
            )}

            {view === "malformed" && (
              <Outcome
                icon={<AlertTriangle className="h-5 w-5 text-loss" />}
                title="This link is damaged"
                titleClass="text-loss"
                body={
                  <>
                    The token in the link doesn&rsquo;t look like one we issue — mail clients
                    sometimes truncate or line-wrap long URLs. Try copying the complete link from
                    the email, or request a fresh one below.
                  </>
                }
                action={<ResendBlock />}
              />
            )}

            {view === "unavailable" && (
              <Outcome
                icon={<AlertTriangle className="h-5 w-5 text-flat" />}
                title="Server unreachable"
                titleClass="text-flat"
                body={<>The token was not judged — nothing was consumed. Try again in a moment.</>}
                action={
                  tokenRef.current ? (
                    <Button variant="default" onClick={() => void run(tokenRef.current!)}>
                      Try again
                    </Button>
                  ) : undefined
                }
              />
            )}

            {view === "no_token" && (
              <Outcome
                icon={<MailQuestion className="h-5 w-5 text-zinc-400" />}
                title="Nothing to verify"
                titleClass="text-zinc-200"
                body={
                  <>
                    This page confirms an operator email address. Open it from the link in the
                    verification email — the token rides in the link&rsquo;s{" "}
                    <code className="text-zinc-300">#fragment</code>, which never appears in any
                    server or proxy log, so the URL must arrive intact from your mail client.
                  </>
                }
                action={<ResendBlock />}
              />
            )}
          </CardBody>
        </Card>

        <p className="mt-5 text-center text-[11px] leading-relaxed text-zinc-600">
          Verifying proves the alert channel works. It never signs you in — that always takes the
          password and a second factor.
        </p>
      </div>
    </div>
  );
}

function Outcome({
  icon,
  title,
  titleClass,
  body,
  action,
}: {
  icon: React.ReactNode;
  title: string;
  titleClass: string;
  body: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div>
      <div className={`flex items-center gap-2 text-sm font-medium ${titleClass}`}>
        {icon}
        {title}
      </div>
      <p className="mt-2 text-sm leading-relaxed text-zinc-400">{body}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

function SignInLink() {
  return (
    <Link href="/login" className="inline-block">
      <Button variant="brass">Sign in</Button>
    </Link>
  );
}

/** Resend request. §5.2 DOES bind this control (it takes an address, not a
 *  token): the server answers an identical 202 whether the address is unknown,
 *  already verified, or cooldown-suppressed — so this UI shows one neutral
 *  confirmation and never implies whether a mail was actually sent. */
function ResendBlock() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<"idle" | "accepted" | "unavailable">("idle");

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    const r = await resendVerification(email.trim());
    setBusy(false);
    setState(r);
  }

  if (state === "accepted") {
    return (
      <p className="text-sm leading-relaxed text-zinc-400">
        Request accepted. If that address belongs to an operator, a fresh link is on its way — the
        response is identical either way, and rapid repeats are quietly rate-limited. That
        vagueness is deliberate.
      </p>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3">
      <div>
        <label htmlFor="resend-email" className="mb-1.5 block text-xs uppercase tracking-widest text-zinc-500">
          Need a new link?
        </label>
        <input
          id="resend-email"
          type="email"
          required
          autoComplete="username"
          spellCheck={false}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className={inputCls}
          placeholder="operator@…"
        />
      </div>
      {state === "unavailable" && (
        <div role="alert" className="text-sm text-loss">
          The server could not be reached — try again.
        </div>
      )}
      <Button type="submit" variant="default" disabled={busy}>
        {busy ? (
          <>
            <Spinner /> Sending…
          </>
        ) : (
          <>
            <Send className="h-4 w-4" /> Resend verification email
          </>
        )}
      </Button>
    </form>
  );
}
