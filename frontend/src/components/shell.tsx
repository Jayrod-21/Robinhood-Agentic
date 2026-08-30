"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import useSWR from "swr";
import { Activity, BookOpen, FlaskConical, Gavel, GraduationCap, LayoutDashboard, LineChart, ListFilter, LogOut, Newspaper, NotebookPen, Scale, SlidersHorizontal, Target } from "lucide-react";
import { fetchAuthState, fetchMe, logout } from "@/lib/auth";
import { cn } from "@/lib/format";
import { DataTrustStrip } from "@/components/data-trust";
import { ChatDrawer } from "@/components/chat-drawer";

/** Pages that must render for a signed-out visitor. Redirecting away from these
 *  would loop: /login is where the redirect SENDS people, and /verify-email is
 *  reached from an email link before any session exists. */
const PUBLIC_PATHS = new Set(["/login", "/verify-email"]);

const NAV = [
  { href: "/", label: "Portfolio", icon: LayoutDashboard },
  { href: "/reconciliation", label: "Reconcile", icon: Scale },
  { href: "/performance", label: "Performance", icon: LineChart },
  { href: "/calibration", label: "Calibration", icon: Target },
  { href: "/learning", label: "Learning", icon: GraduationCap },
  { href: "/testing-lab", label: "Testing Lab", icon: FlaskConical },
  { href: "/fundamentals", label: "Fundamentals", icon: BookOpen },
  { href: "/journal", label: "Journal", icon: NotebookPen },
  { href: "/market", label: "Market", icon: Newspaper },
  { href: "/scan", label: "Scan", icon: ListFilter },
  { href: "/pipeline", label: "Pipeline", icon: Activity },
  { href: "/debate", label: "Debate", icon: Gavel },
  { href: "/settings", label: "Parameters", icon: SlidersHorizontal },
];

/** Send a signed-out visitor to the sign-in page.
 *
 *  Without this the app had no route to its own login form: the dashboard
 *  rendered, every data call 401'd, and the page showed an error with no link
 *  and no redirect — the only way in was to type /login by hand. The route
 *  tests confirmed /login existed and served 200, which was true and useless;
 *  nothing tested that a person could REACH it.
 *
 *  Redirects only on `anonymous` (an explicit 401). During an auth-database
 *  outage /api/auth/me answers 503 fail-closed, and bouncing to a login page
 *  that also cannot work would turn an outage into a redirect loop — so
 *  `indeterminate` deliberately does nothing and leaves the page's own error
 *  state visible. Same for the pre-auth deployment where AUTH_DATABASE_URL is
 *  unset: the dashboard keeps working exactly as it did before auth existed.
 *
 *  `next` carries the path being visited so the login page can return there
 *  instead of dumping everyone on the dashboard root. */
function useRedirectWhenSignedOut(pathname: string) {
  useEffect(() => {
    if (PUBLIC_PATHS.has(pathname)) return;
    let cancelled = false;
    void (async () => {
      const state = await fetchAuthState();
      if (cancelled || state.kind !== "anonymous") return;
      const next = `${window.location.pathname}${window.location.search}`;
      // Full navigation, not router.push: every client cache must restart cold
      // so no stale authenticated data survives into the signed-out view.
      window.location.assign(`/login?next=${encodeURIComponent(next)}`);
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname]);
}

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  useRedirectWhenSignedOut(pathname);
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-ink-800 bg-ink-900/50 px-3 py-5 md:flex">
        <div className="px-2">
          <div className="font-serif text-xl text-zinc-100">Agentic</div>
          {/* Deliberately not the account number: this is a static layout shell with no API data,
              so a hard-coded identifier here can silently disagree with the real account. The
              Portfolio page renders the live `account_masked` value from /api/account instead.
              The same logic caps this line at the broker of record (Alpaca paper): the snapshot's
              live `source` — which is what discloses the Robinhood-file fallback when Alpaca
              credentials are absent — is rendered on the Portfolio page, not hard-coded here. */}
          <div className="text-[11px] uppercase tracking-[0.2em] text-brass">Alpaca Paper</div>
        </div>
        <nav className="mt-8 flex flex-col gap-1">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                  active ? "bg-ink-800 text-zinc-100" : "text-zinc-400 hover:bg-ink-850 hover:text-zinc-200",
                )}
              >
                <Icon className="h-4 w-4" />
                {label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-auto">
          <SignOutControl />
          <div className="px-2 text-[11px] leading-relaxed text-zinc-600">
            Read-only monitor. Live debates cost API tokens. Trades execute via the in-session agent.
          </div>
        </div>
      </aside>
      <main className="flex-1 overflow-x-hidden px-4 py-6 sm:px-8">
        {/* Always-visible honesty bar: how fresh/real/covered the data is. Hidden on the signed-out
            public pages, where there is no account data to speak to and the strip would just be noise. */}
        {!PUBLIC_PATHS.has(pathname) && <DataTrustStrip />}
        {children}
      </main>
      {/* Ask-Claude drawer: reachable from every page, hidden on the signed-out public pages. */}
      {!PUBLIC_PATHS.has(pathname) && <ChatDrawer />}
    </div>
  );
}

/** Sign-out — the shell's only auth surface. Auth state comes from
 *  GET /api/auth/me (docs/AUTH_THREAT_MODEL.md §4): the __Host-rh_sid cookie is
 *  HttpOnly and can never be inspected from JS. fetchMe resolves null when
 *  logged out, when the session expired or was revoked, and while the auth
 *  backend does not exist yet — in every one of those cases this control simply
 *  stays hidden, so the shell degrades cleanly today. */
function SignOutControl() {
  const { data: me } = useSWR("auth:/api/auth/me", () => fetchMe(), {
    shouldRetryOnError: false,
  });
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  if (!me) return null;

  async function onSignOut() {
    if (busy) return;
    setBusy(true);
    setFailed(false);
    // A logout is the server stamping revoked_at on the session row (§5.3).
    // The cookie is out of JS reach, so redirecting without a confirmed
    // revocation would fake a signed-out state over a live session — hence the
    // honest failure message instead of an unconditional redirect.
    const ok = await logout();
    if (ok) {
      // Full navigation so every client cache (SWR included) restarts cold.
      window.location.assign("/login");
    } else {
      setBusy(false);
      setFailed(true);
    }
  }

  return (
    <div className="mb-3 border-t border-ink-800 px-2 pt-3">
      <div className="truncate text-[11px] text-zinc-500" title={me.email}>
        {me.email}
      </div>
      <button
        onClick={() => void onSignOut()}
        disabled={busy}
        className="mt-1.5 flex items-center gap-2 rounded-lg text-sm text-zinc-400 transition-colors hover:text-zinc-100 disabled:opacity-50"
      >
        <LogOut className="h-4 w-4" />
        {busy ? "Signing out…" : "Sign out"}
      </button>
      {failed && (
        <p className="mt-1 text-[11px] leading-snug text-loss" role="alert">
          The server didn&rsquo;t confirm the sign-out — this session is still live. Try again.
        </p>
      )}
    </div>
  );
}

export function PageHeader({ title, subtitle, right }: { title: string; subtitle?: string; right?: React.ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="font-serif text-2xl text-zinc-100">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-zinc-500">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}
