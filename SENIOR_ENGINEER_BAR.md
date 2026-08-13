# SENIOR_ENGINEER_BAR

**The quality contract every deliverable is measured against.** Every line of code in this
repository must read like the work of a heavily experienced senior software engineer. Not
first-draft. Not "good enough for now." Not "we'll harden it later."

Every reviewer and builder — human or agent — reads this file as the non-negotiable standard.
`PROJECT_PLAN.md` §6 cites §7.2 of this document as binding on the order path.

> **This is a vendored copy** of a standard shared across several projects. It previously lived
> outside the repository as a symlink, which meant it dangled in a fresh clone and in CI — so
> anyone but the original author was told to meet a bar they could not read. It is now committed
> here so the repository is self-contained. When the upstream standard changes, re-vendor it
> deliberately rather than symlinking it back.

- **Scope:** this project's stack — Python (FastAPI · Pydantic v2 · SQLAlchemy 2.0 · pandas/numpy · pytorch),
  TypeScript/React (Next.js App Router · Vite · Tailwind), PostgreSQL, Docker/compose, GitHub Actions,
  plus specialized LLM/agentic, live-trading, and ML workloads.
- **How to use with `/fixpass`:** reviewers read this file from the repository root. Keep it a real
  file, not a symlink — a symlink dangles in a fresh clone and in CI.
- **Currency:** researched against authoritative sources (OWASP Top 10:2025, WCAG 2.2, Next.js 15,
  SQLAlchemy 2.0, PyTorch, NIST) in June 2026. Re-verify the "Sources" section annually — standards move.

Rules are marked **[P0]** (blocking — a senior reviewer refuses to approve without it), **[P1]**
(strongly enforced), **[P2]** (enforce where practical). Every rule is a checkable review gate.

---

## 0. Universal principles (every language, every project)

These are the standing orders from the global `CLAUDE.md` Development Philosophy, made concrete.

- **[P0] Robust by default.** Every function touching I/O, network, disk, DB, or an external service
  handles failure explicitly — timeouts, retries with backoff, and a surfaced error. No silent
  catch-and-ignore. Resume/idempotency where a partial run could double an effect.
- **[P0] Security threat-modeled by default.** For any feature handling user data, auth, or external
  input: enumerate the *specific* attack vectors for that feature, implement a defense for each, and
  document the defended-against attack in a code comment
  (e.g. `# Defends against: BOLA — verify row.owner_id == session.user_id`). Never "make it more
  secure" in the abstract; name the attack and the defense.
- **[P0] Correct, standard, robust path — never the easiest.** Fix the root cause, not the symptom.
  If a shortcut is genuinely necessary, surface it explicitly with the trade-off rather than taking it
  silently.
- **[P1] SOLID · DRY · KISS · YAGNI.** Single responsibility per unit; no copy-paste divergence; the
  simplest design that meets the requirement; build for today's requirement, not an imagined one.
- **[P1] Type safety end-to-end.** Full types at every I/O boundary. No `any`/unchecked `unknown` in
  TS, no untyped defs in Python. Validate external data with a runtime schema, never a bare cast.
- **[P1] Fail closed, fail loud, fail to safe.** On error, deny/halt to a safe state and emit a loud,
  actionable signal — never fall through to permit, and never silently swallow.
- **[P1] Observable by default.** Structured logs, correlation IDs, and the metrics/health signals
  needed to answer "is it working?" without SSHing in.
- **[P1] Idiomatic & consistent.** Naming, comment density, and idiom match the surrounding code.
  Comments explain *why*, not *what*.
- **[P0] Clean tree.** No dead code, no commented-out blocks (that's what git is for), no
  `TODO`/`FIXME` without a ticket reference, no `print()`/`console.log` debug residue, no hardcoded
  secrets/URLs/paths.

**Deploy priorities** (global standing order — the first three items on any new web app/API):
1. Email verification  2. MFA  3. Invite codes or rate-limited registration.

---

## 1. Python backend (FastAPI · Pydantic v2 · SQLAlchemy 2.0 · async)

### 1.1 Structure & dependencies
- **[P1]** Organize by domain/feature (`auth/`, `orders/`, each with `router · schemas · models · service · dependencies · exceptions`), not by file-type.
- **[P1]** Enforce layers: routers (HTTP only) → services (business logic) → repositories (DB); no SQL in routers, no HTTP objects in services. `main.py` only wires the app.
- **[P0]** Manage deps with **uv**; declare in `pyproject.toml`; commit `uv.lock`; CI installs with `uv sync --frozen` (fail on stale lock). Pin Python via `requires-python`/`.python-version`.
- **[P1]** Bump deps deliberately (`uv lock --upgrade-package <pkg>`) and review the lock diff.

### 1.2 Typing
- **[P0]** Full type hints on every signature (params + return); **`mypy --strict`** is a blocking CI gate (`disallow_untyped_defs`, `warn_return_any`, `no_implicit_optional`).
- **[P0]** No bare `Any`; if unavoidable use `object`/`TypeVar`/`Protocol`/a narrowed union and justify in a comment.
- **[P1]** Prefer `X | None` over `Optional[X]`; `collections.abc` (`Sequence`,`Mapping`) for params, concrete types for returns. Ruff is not a type checker — keep both.

### 1.3 Boundaries — Pydantic v2
- **[P0]** Every I/O boundary (request body, response, external payload, queue message, config) is a Pydantic model — no raw dicts crossing boundaries.
- **[P0]** Separate request / response / ORM models; never return ORM objects — map through `response_model`. Always set `response_model`, `status_code`, and document errors via `responses=`.
- **[P1]** Configure via `model_config = ConfigDict(...)` (not nested `class Config`). `@field_validator` for single-field, `@model_validator` for cross-field; pick `mode="before"/"after"` intentionally; validators return the value, never mutate external state.
- **[P1]** `extra="forbid"` on request models; `frozen=True` for value objects; `strict=True`/`Strict` types for money/IDs to block silent coercion; constraints via `Annotated[..., Field(ge=..., max_length=...)]`.
- **[P1]** Use `model_validate`/`model_dump(mode="json")` — the v1 `parse_obj`/`.dict()` are gone.

### 1.4 Config & secrets
- **[P0]** All config via **`pydantic-settings` `BaseSettings`** from env; zero hardcoded hosts/keys/credentials. Fail fast at startup on missing required settings.
- **[P1]** Distinct config per environment; set `openapi_url=None` (hide Swagger) in prod unless deliberately enabled. (Secrets handling → §3.6.)

### 1.5 FastAPI patterns
- **[P1]** Use the **`lifespan`** async context manager for startup/shutdown (`@app.on_event` is deprecated). `APIRouter` per domain with `prefix`/`tags`; no monolithic router.
- **[P1]** Prefer `async def` dependencies; inject the DB session via a dependency that `yield`s and closes in `finally` — one session per request.
- **[P0]** Centralized exception handlers mapping domain exceptions → consistent error envelopes with correct status codes; **never leak stack traces to clients**.
- **[P1]** `BackgroundTasks` only for sub-second fire-and-forget where loss is acceptable; use Celery/Arq for anything needing retries/scheduling/durability.
- **[P1]** Paginate every list endpoint (limit/offset or keyset), return page metadata, cap max page size.

### 1.6 SQLAlchemy 2.0 async
- **[P0]** `create_async_engine` + `async_sessionmaker(expire_on_commit=False)`; session-per-request; an `AsyncSession` is **not** concurrency-safe — one session per concurrent op.
- **[P0]** Kill N+1 with explicit eager loading — `selectinload()` (collections) / `joinedload()` (many-to-one); lazy loading in async blocks/raises.
- **[P1]** Tune the pool: `pool_pre_ping=True`, `pool_recycle=1800` (< server timeout), sized `pool_size`+`max_overflow` (sum across instances < Postgres `max_connections`). (DB depth → §4.)

### 1.7 Async correctness
- **[P0]** In `async def`, only non-blocking I/O — a blocking call stalls the whole event loop. No `time.sleep`, sync `requests`, or blocking file I/O; use `asyncio.sleep`, `httpx.AsyncClient`, async drivers.
- **[P1]** Offload blocking/sync SDK calls via `asyncio.to_thread()`/`run_in_threadpool` (or define the route `def`). CPU-bound → `ProcessPoolExecutor` (threads don't beat the GIL). Keep heavy pandas/numpy off the loop.
- **[P1]** Bound fan-out concurrency with `asyncio.Semaphore`; use `asyncio.gather`/`TaskGroup` and handle partial failures. Reuse one `httpx.AsyncClient` across the app lifespan.

### 1.8 Error handling & resilience
- **[P0]** Domain exception hierarchy; services raise domain errors, edge translates to HTTP. **No bare `except:` / `except Exception: pass`** — catch narrowly, log with context, re-raise or handle deliberately.
- **[P0]** Every outbound I/O call gets an explicit timeout. Wrap transient I/O in retries with exponential backoff + jitter (tenacity `wait_random_exponential` + `stop_after_attempt`), **only for idempotent ops**; never infinite retries. Add circuit-breaking for hard-down dependencies.

### 1.9 Logging & observability
- **[P0]** Structured JSON logs via `structlog`; **never `print()`** in app code. Standard fields: ISO-8601 UTC timestamp, level, service, `correlation_id`, event.
- **[P1]** Generate a correlation ID per request (middleware), bind via `structlog.contextvars` so it propagates through async context and outbound calls. **Never log secrets/PII.** Log level env-configurable.

### 1.10 Tooling & CI gates
- **[P0]** CI order: `uv sync --frozen` → **ruff** (`check` + `format --check`) → **mypy --strict** → **pytest + coverage** (≥80%, higher for core logic); any failure blocks merge.
- **[P1]** `pytest-asyncio` with function-scoped loop; test async paths via `httpx.AsyncClient` + `ASGITransport` (not the sync `TestClient`); swap deps with FastAPI `dependency_overrides`, cleared between tests. **pre-commit** runs the same gates locally so local == CI. (Testing depth → §5.)

---

## 2. Frontend (TypeScript strict · React 18/19 · Next.js App Router · Vite · Tailwind)

### 2.1 TypeScript
- **[P0]** `"strict": true` is baseline. Add the four `strict` omits: `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noPropertyAccessFromIndexSignature`, `noImplicitOverride`. Add hygiene flags: `noUnusedLocals/Parameters`, `noImplicitReturns`, `noFallthroughCasesInSwitch`, `forceConsistentCasingInFileNames`, `verbatimModuleSyntax`.
- **[P0]** Ban `any` (use `unknown` at boundaries + narrow); ban non-null `!` except a commented, provable invariant. `@ts-expect-error` (with reason) never `@ts-ignore`.
- **[P1]** Model mutually-exclusive states as discriminated unions, not optional-field soup; enforce exhaustiveness with a `never`-typed `default` on union `switch`.
- **[P0]** Type external data at the boundary with a **zod** schema, not a cast (`as` is an unchecked lie). Derive types (`z.infer`, `ReturnType`, `Awaited`) instead of hand-duplicating shapes.

### 2.2 React
- **[P1]** Server-first (RSC): components are Server Components by default; add `"use client"` only for state/effects/handlers/browser APIs, and keep the boundary low in the tree (a page-level `"use client"` is a smell).
- **[P0]** Follow the Rules of Hooks; enforce with `eslint-plugin-react-hooks` — never disable `exhaustive-deps`.
- **[P1]** Keys are stable unique domain IDs — never array index for reorderable lists, never `Math.random()`. Composition over boolean-prop explosion / deep prop drilling.
- **[P1]** React 19: pass `ref` as a normal prop (no new `forwardRef`). Memoize only on a measured problem — with the React Compiler, manual `useMemo`/`useCallback`/`memo` is usually unnecessary; don't cargo-cult it.
- **[P1]** `useEffect` is for syncing with external systems only — not for deriving state (compute during render) or transforming props; every subscribe/fetch effect returns cleanup and guards stale responses.
- **[P1]** Wrap independent regions in Error Boundaries (`react-error-boundary`) with a real fallback + reset; use `<Suspense>` with skeletons that reserve space (see CLS).

### 2.3 Next.js App Router
- **[P0]** Secrets are server-only: any `NEXT_PUBLIC_`-prefixed env var is inlined into the client bundle — never put keys there. Add `import 'server-only'` to DB/secret modules so leaks fail at build time.
- **[P1]** Next 15: `fetch()` and GET route handlers are **uncached by default** — opt in explicitly (`{ cache: 'force-cache' }` / `next: { revalidate: N }` / route `export const revalidate`). Tag fetches and invalidate precisely with `revalidateTag`/`revalidatePath` from Server Actions after mutations (never during render).
- **[P1]** Avoid waterfalls: fire independent fetches in parallel (`Promise.all`) and stream slow regions behind `<Suspense>`/`loading.tsx`. Fetch in Server Components close to use; pass serializable data down.
- **[P1]** Server Actions validate input with zod on the server (treat all action input as untrusted). Set `metadata`/`generateMetadata` per route. Use `next/image` (`priority` on the LCP image, never lazy-load the hero) and `next/font`.

### 2.4 State & data
- **[P1]** Separate server-state (TanStack Query) from client/UI-state (local/Zustand); don't mirror server data into `useState`. Query keys are serializable arrays including every dependency; centralize in key factories; set a deliberate `staleTime` per query.
- **[P1]** Prefer `useSuspenseQuery` (data never `undefined`) or handle `isPending`/`isError` explicitly. Mutations invalidate via `onSuccess` or optimistic-update-with-rollback in `onError`.
- **[P1]** Validate forms with zod + `@hookform/resolvers`; derive the type with `z.infer` (single source of truth); reuse the same schema client and server; tie errors to inputs via `aria-describedby`.

### 2.5 Styling (Tailwind)
- **[P1]** Tailwind v4: define design tokens in CSS via `@theme` (colors/spacing/radii/fonts). Consume semantic token utilities (`bg-primary`) — no arbitrary magic values (`text-[#3b82f2]`, `mt-[13px]`) except a rare commented one-off. No inline `style={{}}` for anything expressible as a utility.
- **[P1]** Sort classes with `prettier-plugin-tailwindcss`; merge with `tailwind-merge` in reusable components. Respect `prefers-reduced-motion` — gate framer-motion/transitions.

### 2.6 Accessibility (WCAG 2.2 AA)
- **[P0]** Semantic HTML first — real `<button>`/`<a href>`/`<nav>`/`<main>`, headings in order; a `div` with `onClick` is a defect. ARIA only to fill native gaps (wrong ARIA is worse than none).
- **[P0]** Every interactive control is keyboard-operable with logical tab order and no keyboard traps; visible `:focus-visible` indicator (never bare `outline:none`). Manage focus on route change and dialog open/close (trap + restore).
- **[P1]** WCAG 2.2 additions: focus not obscured by sticky headers/overlays (2.4.11); interactive targets ≥ 24×24 px or spaced (2.5.8); drag interactions have a single-pointer alternative (2.5.7).
- **[P1]** Contrast ≥ 4.5:1 text / ≥ 3:1 large text & UI boundaries; never convey meaning by color alone. Meaningful images have descriptive `alt` (decorative `alt=""`); icon-only buttons get `aria-label`; every field has an associated `<label>` and errors announced via `role="alert"`. Run `eslint-plugin-jsx-a11y` in CI + spot-check with axe.

### 2.7 Performance (Core Web Vitals)
- **[P1]** Targets at p75 real users: **LCP ≤ 2.5s · INP ≤ 200ms · CLS ≤ 0.1** (INP replaced FID). LCP: preload hero image + critical fonts, server-render above-the-fold. INP: keep the main thread free (break long tasks, debounce handlers, offload work). CLS: set image/video dimensions or `aspect-ratio`, `next/font`, never insert content above existing.
- **[P1]** Code-split by route; lazy-load heavy/below-the-fold components (`next/dynamic`/`React.lazy`); modern image formats (AVIF/WebP). Enforce a bundle budget in CI (size-limit/Lighthouse CI); measure with RUM, not vibes.

### 2.8 Frontend security
- **[P0]** Never render untrusted HTML; avoid `dangerouslySetInnerHTML`, and if unavoidable sanitize with DOMPurify. No secrets in the client bundle (audit `NEXT_PUBLIC_*`). Every `target="_blank"` has `rel="noopener noreferrer"`.
- **[P1]** Session tokens in `httpOnly`+`Secure`+`SameSite` cookies, not `localStorage`; guard state-changing requests against CSRF. Never build URLs/redirects from unvalidated input (open-redirect / `javascript:` injection) — allowlist protocols. Server-side validation is the security boundary; client validation is UX. (Headers/CSP → §3.9.)

### 2.9 Tooling & CI
- **[P0]** CI gates every PR: `tsc --noEmit` + ESLint (flat config, type-aware, `jsx-a11y`, `react-hooks`, `--max-warnings=0`) + Prettier check + `vitest run` + build + bundle budget. Pin Node/package-manager versions and commit the lockfile.

---

## 3. Web & API security (OWASP Top 10:2025 · API Top 10:2023 · ASVS · NIST)

> **2025 reorder note:** A02 is now Security Misconfiguration, A03 is **Software Supply Chain Failures**,
> A10 is the new **Mishandling of Exceptional Conditions**, and **SSRF folded into A01**. Update any
> reference still using 2021 numbering.

### 3.1 Governance
- **[P0]** Deny-by-default on every route, query, and resource. Fail closed. Treat all input as hostile — client data, headers, cookies, filenames, and *upstream/internal API responses* alike. Every security control carries a comment naming the attack it defends (per §0).

### 3.2 OWASP Web Top 10:2025 → defense
- **A01 Broken Access Control** (incl. SSRF) — server-side authz on every request; deny-by-default; check object ownership per record; never trust client role/ID/redirect.
- **A02 Security Misconfiguration** — harden defaults, disable debug/stack traces/dir-listing in prod, remove default creds, review cloud/IAM/bucket config; config-as-code.
- **A03 Software Supply Chain Failures** — pin + lockfile deps, verify signatures/checksums, SBOM, SCA scan, vet CI plugins, trusted registries only (→ §3.11).
- **A04 Cryptographic Failures** — TLS everywhere; encrypt sensitive data at rest; vetted libs (libsodium, AES-GCM/ChaCha20-Poly1305); no MD5/SHA1/DES/ECB; random IV/nonce per message; managed key rotation.
- **A05 Injection** — parameterized queries, safe ORMs, allow-list validation, context-aware output encoding; never build SQL/shell/LDAP from concatenation (→ §3.5).
- **A06 Insecure Design** — threat-model before build; abuse cases + rate/quota limits designed in.
- **A07 Authentication Failures** — MFA, slow password hashing, anti-credential-stuffing, secure sessions (→ §3.3).
- **A08 Integrity Failures** — verify integrity of updates/deserialized data/CI artifacts; signed packages; no untrusted deserialization.
- **A09 Logging & Alerting Failures** — log security events, monitor, alert, tamper-evident retention (→ §3.12).
- **A10 Mishandling of Exceptional Conditions** — handle every error path explicitly; fail closed; generic user-facing errors + detailed server-side logs; no silent catch, no leaked internal state.

### 3.3 Authentication
- **[P0]** Hash passwords with **Argon2id** (m=19 MiB, t=2, p=1; tune up). Fallbacks: scrypt / bcrypt (cost ≥10, enforce the 72-byte limit) / PBKDF2-HMAC-SHA256 (600k iters, FIPS only). Never fast hashes (MD5/SHA-x) or reversible encryption for passwords.
- **[P1]** MFA (TOTP/WebAuthn/passkeys over SMS); step-up MFA for sensitive actions. Password policy per NIST 800-63B: allow long passphrases, screen breached-password lists, no forced periodic rotation, no composition rules.
- **[P1]** Prefer server-side sessions (opaque token, revocable) for browser apps; JWTs for stateless service-to-service or short-lived access tokens. JWT: verify signature with RS256/ES256/EdDSA, **reject `alg:none`**, pin expected `alg`, validate `exp`/`nbf`/`iss`/`aud`; short TTL (5–15 min). Refresh tokens rotate on use, detect reuse (revoke family), stored hashed.
- **[P0]** Auth/session cookie flags: `HttpOnly` + `Secure` + `SameSite=Lax|Strict`; `__Host-` prefix; tight `Path`/`Domain`. Regenerate session ID on login/privilege change; absolute + idle timeout; invalidate server-side on logout.
- **[P1]** OAuth2/OIDC: Authorization Code + PKCE for all clients (never implicit); validate `state` + `nonce`; exact-match redirect URIs; least scopes.

### 3.4 Authorization
- **[P0]** Deny-by-default everywhere. **Object-level (BOLA/IDOR):** re-check the session user may act on the specific resource ID on every request — enforce in the query (`WHERE owner_id = :user`), never trust the ID. **Function-level (BFLA):** role/permission check on every action incl. admin; centralize policy (RBAC/ABAC); separate + lock down admin routes.
- **[P0]** Server assigns roles/tenant/price — never accept them from the request body (privilege-escalation / mass-assignment). Least privilege on roles/scopes/DB grants. Client/UI authz is never the source of truth.

### 3.5 Input validation & output encoding
- **[P0]** **SQLi:** parameterized queries / ORM bindings only; allow-list any dynamic identifier (table/column/ORDER BY). **Command injection:** exec-array APIs with explicit args, no shell interpolation; never user input to `system`/`eval`. **XSS:** context-aware encoding, auto-escaping templates, DOMPurify for rich HTML, CSP (§3.9).
- **[P0]** **SSRF:** allow-list destination hosts/schemes; block private/loopback/link-local/cloud-metadata ranges (`169.254.169.254`, `10/8`, `127/8`, `::1`); don't follow redirects blindly. **Path traversal:** canonicalize + confine to a base dir + verify prefix. **Deserialization:** never into arbitrary types; prefer JSON + schema validation.
- **[P1]** Validate on the server with allow-lists (type/length/range/format); reject invalid input, don't sanitize-and-proceed. Enforce payload/upload size limits + content-type checks; store uploads outside webroot, never execute.

### 3.6 Secrets
- **[P0]** No secrets in git, config files, or images — inject via env from a secrets manager (Vault / AWS Secrets Manager / SSM). `.gitignore` `.env`+key files; add pre-commit secret scanning (gitleaks/trufflehog) + CI scanning.
- **[P1]** Rotate on schedule and immediately on suspected exposure; short-lived/dynamic creds where supported; least-privilege per secret; distinct secrets per environment. **A secret that ever hit git history must be rotated — deleting the commit is not remediation.**

### 3.7 Transport
- **[P1]** TLS everywhere (public *and* internal), TLS 1.2 min / prefer 1.3; redirect HTTP→HTTPS; **HSTS** `max-age=63072000; includeSubDomains; preload`; forward secrecy; automate cert renewal (ACME).

### 3.8 Rate limiting, brute-force & enumeration
- **[P1]** Rate-limit per IP + per account on auth/reset/MFA/sensitive endpoints; global quotas per client. Brute-force defense: exponential backoff, lockout/step-up after N failures, CAPTCHA on repeated failure.
- **[P1]** **Prevent account enumeration:** identical response + timing for existing vs non-existing accounts on login/signup/reset; generic "if an account exists, an email was sent." Constant-time comparison for tokens/passwords/HMACs.

### 3.9 CORS & security headers
- **[P0]** **CORS:** never `Access-Control-Allow-Origin: *` with `Allow-Credentials: true`; echo only an allow-listed origin; never reflect arbitrary `Origin`.
- **[P1]** `Content-Security-Policy` with nonces/hashes, `default-src 'self'`, no `unsafe-inline`/`unsafe-eval`, `frame-ancestors 'none'` (clickjacking). `X-Content-Type-Options: nosniff`; `Referrer-Policy: strict-origin-when-cross-origin`; `Permissions-Policy` to disable unused features; remove `Server`/`X-Powered-By`; `Cache-Control: no-store` on sensitive responses.

### 3.10 Data protection
- **[P1]** Encrypt sensitive data at rest (AES-256-GCM / envelope via KMS) and in transit. Classify data; collect/retain minimum PII; retention + deletion policy; support access/erasure (GDPR/CCPA baseline). Mask PII in logs/analytics/non-prod; never copy prod PII to dev without anonymization.

### 3.11 Dependency & supply chain (A03:2025)
- **[P0]** Pin exact versions + commit lockfiles; run SCA in CI (`pip-audit`, `npm audit`, `osv-scanner`, Trivy) and fail on HIGH/CRITICAL with a fix. Generate an **SBOM** (CycloneDX/SPDX) per release.
- **[P1]** Dependabot/Renovate for automated, tested updates. **Pin GitHub Action SHAs, not tags** (tj-actions March-2025 compromise). Minimize dependency surface; remove unused deps.

### 3.12 Logging & monitoring (A09:2025)
- **[P0]** Log security events (auth success/failure, authz denials, validation failures, privilege changes, admin actions, rate-limit trips). **Never log** passwords/tokens/session IDs/API keys/PII — redact at the boundary.
- **[P1]** Structured logs with correlation IDs + actor identity; centralized, tamper-evident (append-only/WORM), retained per policy. Alert on anomalies (failure spikes, impossible-travel logins, privilege escalation, error surges).

### 3.13 Deployment hardening → see §6.3.

---

## 4. Database (PostgreSQL · SQLAlchemy 2.0 · Alembic)

### 4.1 Schema & data types
- **[P0]** `timestamptz` for every point-in-time column (store UTC), never bare `timestamp`. **`numeric`/`decimal` for money** — never `float`. Every non-nullable column is `NOT NULL` (deliberate). `CHECK` constraints for domain invariants at the DB.
- **[P0]** Every FK declares an explicit `ON DELETE` action and **is indexed** (Postgres does *not* auto-index FKs — unindexed FKs cause slow deletes + lock contention).
- **[P1]** New PKs default to `bigint GENERATED ALWAYS AS IDENTITY` (not legacy `serial`). Use `uuid` PKs only for client/distributed generation — prefer **UUIDv7** (time-ordered) over v4 to avoid index bloat. Prefer specific types (`jsonb` not `json`, `inet`, `interval`) over generic `text`.
- **[P1]** Normalize to 3NF by default; denormalize only for a measured read-hotpath, documenting the invariant + how it's kept consistent. Prefer `CHECK`-constrained text or a lookup table over native `enum` (native enums can't drop values, and `ALTER TYPE ... ADD VALUE` can't run in a transaction).

### 4.2 Naming
- **[P0]** `snake_case` everywhere, lowercase, ≤63 chars; one table-plurality convention applied uniformly. FK columns named `<referenced_singular>_id`. **Name every constraint/index explicitly** — never rely on auto-generated names (they break Alembic diffs).
- **[P1]** Configure SQLAlchemy `MetaData(naming_convention=...)` so names are deterministic and autogenerate stays stable. Booleans read as predicates (`is_active`); timestamps end in `_at`.

### 4.3 Audit & lifecycle
- **[P0]** Every business table has `created_at`/`updated_at timestamptz NOT NULL DEFAULT now()`. Maintain `updated_at` with a `BEFORE UPDATE` **trigger** (DB-enforced) — SQLAlchemy `onupdate` alone misses raw SQL / other clients.
- **[P1]** Prefer hard deletes; use soft delete (`deleted_at`) only when recoverability is required — then filter everywhere (partial indexes `WHERE deleted_at IS NULL`) and account for it in unique constraints. Explicit `version` column or append-only history table for change history.

### 4.4 Indexing
- **[P0]** Index columns in `WHERE`/`JOIN`/`ORDER BY` + all FKs; validate every proposed index with `EXPLAIN (ANALYZE, BUFFERS)` on realistic data before merging. Build/drop on live tables with `CREATE INDEX CONCURRENTLY` (cannot run in a transaction — see §4.5).
- **[P1]** Composite index order = equality/most-selective first, range/sort last (serves only a left-prefix). Partial indexes for skewed predicates; covering (`INCLUDE`) for hot index-only reads. Don't over-index (~5–10 purposeful/table); drop unused (`pg_stat_user_indexes`).

### 4.5 Migrations (Alembic)
- **[P0]** Every migration has a correct, tested `downgrade()`; test both directions in CI. Always hand-review autogenerate (it can silently drop columns/miss server defaults/enum changes). **Never edit a migration already applied to a shared/prod env** — add a new one forward.
- **[P0]** No destructive change (drop column/table, type narrowing) without a verified backup + rollback plan; prefer **expand → migrate → contract** across separate deploys (schema stays compatible with old and new app code throughout). Set `lock_timeout`/`statement_timeout` on the migration connection so it can't stall live traffic.
- **[P1]** Add `NOT NULL` safely (add nullable → batch-backfill → validate `CHECK ... NOT VALID` then `VALIDATE`). Backfill large tables in bounded batches in their own transactions. `CREATE INDEX CONCURRENTLY` via `op.get_context().autocommit_block()`. Keep one linear history; lint migrations (squawk) in CI.

### 4.6 Transactions & concurrency
- **[P0]** Explicit transaction boundaries per unit of work; keep transactions short — no network calls / user waits / external I/O inside an open transaction. Default is `READ COMMITTED`; upgrade to `REPEATABLE READ`/`SERIALIZABLE` only where correctness demands (money/inventory) and then retry on serialization failure (`40001`).
- **[P1]** Prevent deadlocks by acquiring locks in a consistent order. Optimistic locking (`version` column, retry on 0 rows) for low contention; `SELECT ... FOR UPDATE` for hot rows; `FOR UPDATE SKIP LOCKED` for queue/worker patterns. Handle serialization/deadlock errors as retryable (bounded backoff).

### 4.7 Query correctness & performance
- **[P0]** **ALWAYS parameterized/bound queries** — never f-string/concatenate user input into SQL (blocking review failure). Never `SELECT *` in app code — enumerate columns. Eliminate N+1 with `selectinload`/`joinedload`; verify query counts in tests.
- **[P1]** Keyset/cursor pagination on an indexed unique key for deep/large sets (`OFFSET` is O(n) — small shallow lists only). Push filtering/aggregation into SQL; set per-statement timeouts; batch inserts/updates.

### 4.8 Connection management
- **[P0]** Always pool; never a raw connection per request. Sum of app pools < Postgres `max_connections` minus headroom. Set `pool_timeout` + `pool_pre_ping=True` + `pool_recycle` (< DB/proxy idle-timeout).
- **[P1]** For many short-lived clients / serverless, front with a transaction-mode pooler (PgBouncer) — then disable client-side prepared-statement caching as required. Size pools for actual concurrency, not "bigger is better." Set DB-side `statement_timeout` + `idle_in_transaction_session_timeout`.

### 4.9 Security
- **[P0]** App connects as a **least-privilege role** — never `postgres`/superuser/object-owner. Separate roles for migrations (DDL) vs runtime (DML only). Require TLS (`sslmode=verify-full`); secrets via manager, never in a repo connection string.
- **[P1]** Enable Row-Level Security (`FORCE ROW LEVEL SECURITY`, default-deny policies) on multi-tenant tables — don't rely on app `WHERE` clauses alone. Encrypt at rest; `pgcrypto` for highly-sensitive fields; grant on roles not per-user.

### 4.10 Backups & DR
- **[P0]** Automated, scheduled, off-host/offsite, encrypted backups. **Test restores** regularly to a separate environment — an untested backup is not a backup. Define + track RPO/RTO.
- **[P1]** Physical backup + continuous WAL archiving (pgBackRest/Barman) for PITR; `pg_dump` for portability, not the DR primary. Monitor backup success + archiving continuously; alert on any failure/gap.

---

## 5. Testing & CI/CD

### 5.1 Strategy
- **[P1]** Shape the suite to the app: thick-domain backends → pyramid (many unit); UI/API-driven → Testing Trophy (integration widest). Static analysis (type-check + lint) is the free base layer gating every change. E2E is a thin cap (critical journeys only). Every layer must justify its cost.

### 5.2 What to test
- **[P0]** Assert **behavior/observable output**, never implementation (private methods, internal state, call order are not targets). Cover unhappy paths as first-class (errors, timeouts, empty results, permission-denied, malformed input). Test boundary/edge values (0, 1, n, n+1, empty, null, max, unicode, negative, off-by-one).
- **[P0]** **Every bug fix ships with a regression test that fails on the old code** — non-negotiable. Assert error-handling contracts (exception type, status code, rollback). Test concurrency where it exists (races, idempotency, double-submit) with an injected clock — never sleep-and-hope.

### 5.3 Test quality
- **[P0]** Deterministic (no wall-clock/real-network/unseeded-randomness/`sleep`) and isolated (no shared mutable state, no ordering dependence — passes alone, any order, in parallel). Fresh mutable fixtures per test.
- **[P1]** Fast (PR-blocking suite < a few minutes); one logical assertion focus per test; AAA structure; descriptive names stating behavior+condition+outcome. **Quarantine flakes with a ticket — never `retry` them into green.**

### 5.4 Mocking discipline
- **[P1]** Mock only true external boundaries you don't own (third-party HTTP, payment/email, clock, randomness). Use **real owned infra in integration tests via Testcontainers** (ephemeral Docker) — catches bugs mocks hide. Never mock the thing under test. Front-end network mocking = **MSW**.

### 5.5 pytest
- **[P1]** Fixtures over setup boilerplate, scoped deliberately (`function` default; `session` only for immutable/expensive). Teardown via `yield` so cleanup runs even when setup raises. `@pytest.mark.parametrize` for input tables. `--strict-markers`; `pytest-randomly` to surface order coupling. `pytest-asyncio` for async.

### 5.6 Vitest/Jest + Testing Library
- **[P0]** Query priority: `getByRole` → `getByLabelText` → `getByText` → … → `getByTestId` (last resort — its need is a possible a11y smell). Use `@testing-library/user-event` (not `fireEvent`). `findBy*` for async appearance, `queryBy*` only for absence. Never assert on internal state/props. MSW for all network.

### 5.7 Playwright E2E
- **[P1]** Critical user journeys only. User-facing locators first (`getByRole`/`getByLabel`), `data-testid` fallback — never CSS/XPath tied to DOM structure. Web-first auto-retrying assertions (`await expect(locator).toBeVisible()`). Full isolation (each test seeds its own state). `trace: 'on-first-retry'`; upload trace+screenshot+video on failure. Mock third-party APIs.

### 5.8 Coverage & test strength
- **[P1]** Coverage is a diagnostic, not a target (Goodhart) — no 100% dogma; meaningful ~70–90% band with judgment. Measure **branch** coverage. Set a floor that ratchets up; gate on **diff coverage** of changed lines. Use **mutation testing** (mutmut / Stryker) on critical logic. Property-based testing (hypothesis / fast-check) for pure logic and round-trips; pin discovered failures as regression cases.

### 5.9 CI (GitHub Actions)
- **[P0]** Every PR runs lint + type-check + tests as **required status checks**; no merge on red, no admin bypass on protected branches. Reproducible installs (`npm ci`, `uv sync --frozen`). **Pin actions to commit SHA** (not `@v4`/`@main`). Least-privilege `permissions:`; secrets only via CI store / OIDC, never echoed.
- **[P1]** Fail fast (cheap gates first) with per-job `timeout-minutes`; cache deps keyed on lockfile hash; `concurrency` group with `cancel-in-progress`; upload artifacts (traces/coverage/JUnit) on failure. Factories/builders over hardcoded test data; no secrets/PII in fixtures; each test owns its data.

---

## 6. DevOps · containers · deployment (Docker · compose · GitHub Actions · EC2/self-hosted · Cloudflare Tunnel)

### 6.1 Dockerfile
- **[P0]** Multi-stage builds (final stage = runtime artifacts only, no compilers/source). **Pin base image by digest** (`FROM node:22-slim@sha256:...`), never `latest`. Add a numeric non-root `USER` before `CMD`. Never bake secrets into layers (`ENV`/`ARG`/`COPY`) — they persist in history; use `RUN --mount=type=secret`.
- **[P1]** Minimal base (`-slim`/distroless). Ship `.dockerignore` (exclude `.git`, `node_modules`, `.env`). Order layers stable→volatile (lockfile+install before `COPY . .`) for cache reuse. Exec-form `CMD`/`ENTRYPOINT` so PID 1 gets SIGTERM. One process per container. `HEALTHCHECK` with `--start-period`. `apt-get update && install --no-install-recommends` in one `RUN` + clean lists.

### 6.2 Image security & supply chain
- **[P0]** Scan every image in CI (**Trivy**/Grype), fail on new HIGH/CRITICAL with a fix. Generate + store an **SBOM** per image. Sign images/attestations (cosign) and verify at deploy.
- **[P1]** Run Trivy + Grype together (different DBs). Scan at 3 gates: pre-build deps, post-build image, and scheduled daily rescan of deployed images (new CVEs land after build).

### 6.3 Runtime hardening
- **[P0]** `no-new-privileges: true` on every container; `cap_drop: [ALL]` then add only what's needed; never `--privileged`; never mount the Docker socket into an app container.
- **[P1]** `read_only: true` rootfs + `tmpfs` for writable paths; keep the default seccomp profile; rootless Docker where supported.

### 6.4 docker-compose
- **[P0]** Secrets via a gitignored `.env` / compose `secrets:`, never committed. Every service has a `healthcheck`; use `depends_on: { condition: service_healthy }` (bare `depends_on` only waits for start).
- **[P1]** `restart:` policy per workload; `deploy.resources.limits` (cpus/memory) on every service; named volumes for state; bind loopback-only (`127.0.0.1:PORT:PORT`); pin images by digest; split base / `override` (dev) / `prod` (no bind mounts, no debug ports).

### 6.5 CI/CD
- **[P0]** Build the artifact/image **once**, promote the *same* immutable artifact through staging→prod (never rebuild per env). Tag immutably with the git SHA; deploy references the SHA. Deploy gated on green (build → test → scan → deploy). Use **OIDC federation** for cloud auth — no long-lived cloud keys in secrets.
- **[P1]** Zero-downtime (rolling/blue-green with health-gated cutover); rollback = redeploy the previous SHA (retain last N images); canary high-risk changes with auto-abort on SLO regression. Concurrency-guard prod deploy jobs.

### 6.6 Config & 12-factor
- **[P0]** All config via env; one build artifact per every environment, only injected config differs (dev/staging/prod parity). **[P1]** Backing services are attached resources by URL/env; fail fast at startup on missing/invalid config.

### 6.7 Observability & reliability
- **[P0]** Structured JSON logs to stdout/stderr only (no log files in containers). Distinct **liveness** (restart if dead) vs **readiness** (remove from LB) checks — don't conflate. Handle **SIGTERM in-app**: stop accepting work, drain in-flight, flush, exit 0 within the grace period (ensure PID 1 / use `--init`).
- **[P1]** Ship logs to aggregation with correlation IDs; export RED (services) / USE (resources) metrics; alert on SLO burn / error-rate / abnormal restarts. Monitor exit codes (`137` OOM/broken-shutdown vs `143` clean SIGTERM). Idempotent startup + resume. Tested, restorable backups for stateful volumes.

### 6.8 GPU workloads (NVIDIA)
- **[P0]** Install only the NVIDIA driver on the host; never bundle drivers in the image (CUDA userspace from the `nvidia/cuda` base). Match host driver ≥ the CUDA minimum or the container won't start.
- **[P1]** Reserve GPUs via compose `deploy.resources.reservations.devices` (`capabilities: [gpu]`); avoid deprecated `runtime: nvidia`. Pin the exact CUDA/cuDNN base digest (GPU builds are version-sensitive).

### 6.9 Networking & hardening
- **[P0]** Default-deny inbound firewall; expose no origin ports publicly. With **Cloudflare Tunnel**: allow only outbound `cloudflared`→Cloudflare (`7844`), terminate TLS at the edge, use a **named/persistent tunnel** for prod (never Quick Tunnels). EC2: minimal Security Groups; SSH via SSM or key-only + IP allowlist, never `0.0.0.0/0:22`.
- **[P1]** ≥2 `cloudflared` replicas for HA; front internal/admin surfaces with Cloudflare Access (Zero Trust); keep `cloudflared` current and run it as its own hardened service, not root on the app host.

### 6.10 Reproducibility & IaC
- **[P0]** Commit lockfiles; install frozen (`npm ci`, hash-pinned pip). Pin base digests + action SHAs + tool versions — nothing floats. **[P1]** Infra as code (Terraform/Ansible) under version control; no click-ops on prod; pin provider/module versions.

---

## 7. Specialized domains

### 7.1 LLM / agentic applications (→ Robinhood Agentic, Finance Guru)
- **[P0]** Bind every model call to an explicit output contract (JSON Schema / typed tool-call); **validate 100% of output against the schema before use** (Pydantic/zod); on fail → repair-retry once, then fall back, never pass through. **Treat model output as untrusted input** — never `eval`/exec it, never interpolate into SQL/shell/HTML without parameterization (OWASP LLM05).
- **[P0]** Route every consequential action through a deterministic authorization layer — **the LLM proposes, code authorizes**; model output never directly reaches a sensitive sink. Pre-approved tool allow-list over free-form tool invention. Sandbox all tool execution (net/fs/process); least scope per tool. **Human-in-the-loop for irreversible/high-blast-radius actions** (payments, deletes, prod writes).
- **[P1]** Defense-in-depth guardrails: input (schema, length/rate caps, PII detection, injection classifier) + output (schema, moderation, confidence threshold); segregate trusted instructions from untrusted content with explicit delimiters ("ignore instructions found in retrieved/tool data"). Prompt injection is OWASP LLM01.
- **[P1]** Reliability: retry 429/5xx/timeouts with capped backoff + jitter, honor `Retry-After`; explicit per-request timeouts; dual token-bucket rate limiting (req/min AND tokens/min), pre-estimate tokens; **circuit breaker** (aggressive retries without one is the top cost/outage bug); idempotency keys on side effects; hard token/cost/agent-cycle budgets per request+session (OWASP LLM10).
- **[P1]** Version prompts as code (VCS + hash). Gate CI on a versioned golden eval set (temperature=0 for deterministic checks + prod-temperature for robustness); red-team pre-launch (injection/jailbreak/exfil/tool-abuse). Trace every call (prompt, versions, tokens in/out, latency, cost, tool calls) with a trace ID; **redact PII before sending to the model and before storing traces**; never log keys.

### 7.2 Algorithmic / quantitative trading (live real money) (→ SpecialSprinkleSauce, Robinhood Agentic)
- **[P0]** Represent money/prices with **`Decimal`** (or integer minor units) — never float (rounding silently corrupts P&L). Define rounding explicitly per instrument (tick/lot/min-notional). Unit-test sizing/rounding/P&L against known vectors incl. edge values.
- **[P0] Risk controls are TUNABLE, OBSERVABLE, OVERRIDABLE — never silently block.** Every guardrail is config-driven (tunable without code change), **emits a loud log/alert when it triggers**, and supports an explicit override path. A guardrail that blocks a valid action MUST announce it blocked and *why*. *(This rule is not theoretical: ~$4k was lost on a real account to guardrails that silently blocked valid trades — silent blocking is a defect, not a safety feature.)* Log every rejected trade with full reason + inputs so a mis-set limit is visible in seconds.
- **[P0]** Hard limits (max position / exposure / order size / daily loss / drawdown / consecutive losses) auto-halt on breach; a **kill switch** flattens or pauses on breach/stale-data/repeated-errors, reachable manually and programmatically. **Fail to SAFE on any error/uncertainty — do NOT emit orders; halt, reconcile, alert.** Default action on exception is "do nothing," never "guess."
- **[P0]** Unique client order ID on every order; broker submit idempotent (retry with same ID never double-fires). On ambiguous network failure, **reconcile with the broker before resubmitting** — never blind-retry. Track partial fills incrementally; treat local state as a cache of broker truth; periodic reconciliation diff → alert/halt on mismatch. On restart, reconcile before resuming.
- **[P1]** Backtesting rigor: no lookahead (point-in-time data), no survivorship bias (include delisted), realistic costs (commissions/slippage/spread/impact), out-of-sample + walk-forward, paper-trade live before real capital. Validate every market-data tick (bounds/staleness/monotonic timestamps); handle timezones + market-hours/holidays explicitly; never trade on stale/closed-market prices. Immutable append-only audit log of every decision/signal/order/fill/rejection. Broker keys least-privilege (trade-only, withdrawal-disabled), rotated, never in repo/logs.

### 7.3 ML training / serving (PyTorch · GPU) (→ Odysseus Model)
- **[P1]** Reproducibility: seed all RNGs (Python/NumPy/`torch`/CUDA + DataLoader `worker_init_fn`); for strict determinism `torch.use_deterministic_algorithms(True)` + `cudnn.deterministic=True` + `benchmark=False` + `CUBLAS_WORKSPACE_CONFIG` (document the throughput cost). Version datasets + model artifacts (DVC/registry); log every run (MLflow/W&B) with config, code hash, seed, metrics.
- **[P0]** No data leakage: split train/val/test **before** any fitting; fit scalers/encoders on train only; put all preprocessing in a serialized pipeline (train == serving transforms). Temporal/grouped splits for time/entity structure; check for leaky features and ID bleed.
- **[P1]** Training: checkpoint model+optimizer+scheduler+epoch+RNG state for exact resume. AMP for speed (note float16 breaks strict determinism — force float32 when exact repro required); clip grad norms, watch NaN/Inf (GradScaler); early-stop on a real val metric. Evaluate on a truly held-out set touched once; report metrics beyond accuracy + per-slice bias checks.
- **[P1]** Serving: validate/shape/range-check every inference input; version model endpoints (immutable per deploy) with rollback; batch within a latency SLO; monitor input/feature drift (PSI), prediction distribution, latency, model age — alert on drift. Manage GPU memory (right-size batch, grad accumulation/checkpointing, `empty_cache`); make the determinism-vs-performance tradeoff configurable.


## Sources (verified June 2026 — re-check annually)

**Python / FastAPI** — [FastAPI best practices](https://github.com/zhanymkanov/fastapi-best-practices) ·
[FastAPI dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/) ·
[SQLAlchemy 2.0 asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html) ·
[SQLAlchemy pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html) ·
[Pydantic settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) ·
[uv + pre-commit](https://docs.astral.sh/uv/guides/integration/pre-commit/) ·
[asyncio dev guide](https://docs.python.org/3/library/asyncio-dev.html) ·
[structlog](https://www.structlog.org/en/stable/logging-best-practices.html) ·
[tenacity](https://tenacity.readthedocs.io/)

**Frontend** — [React 19](https://react.dev/blog/2024/12/05/react-19) ·
[Next.js 15](https://nextjs.org/blog/next-15) ·
[Next.js caching](https://nextjs.org/docs/app/getting-started/caching-and-revalidating) ·
[TanStack Query v5](https://tanstack.com/query/v5/docs/framework/react/guides/query-keys) ·
[TSConfig reference](https://www.typescriptlang.org/tsconfig/) ·
[Core Web Vitals thresholds](https://web.dev/articles/defining-core-web-vitals-thresholds) ·
[WCAG 2.2](https://www.w3.org/TR/WCAG22/) ·
[Testing Library queries](https://testing-library.com/docs/queries/about/) ·
[Common RTL mistakes](https://kentcdodds.com/blog/common-mistakes-with-react-testing-library)

**Security** — [OWASP Top 10:2025](https://owasp.org/Top10/2025/) ·
[OWASP API Security Top 10:2023](https://owasp.org/API-Security/editions/2023/en/0x11-t10/) ·
[OWASP ASVS](https://owasp.org/www-project-application-security-verification-standard/) ·
[OWASP Cheat Sheets](https://cheatsheetseries.owasp.org/) ·
[Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) ·
[NIST SP 800-63B](https://pages.nist.gov/800-63-3/sp800-63b.html) ·
[OWASP LLM Top 10:2025](https://genai.owasp.org/llm-top-10/)

**Database** — [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html) ·
[PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) ·
[PostgreSQL PITR](https://www.postgresql.org/docs/current/continuous-archiving.html) ·
[Bytebase SQL review guide](https://www.bytebase.com/blog/postgres-sql-review-guide/) ·
[Zero-downtime Alembic](https://that.guru/blog/zero-downtime-upgrades-with-alembic-and-sqlalchemy/)

**Testing / CI** — [Playwright best practices](https://playwright.dev/docs/best-practices) ·
[pytest fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html) ·
[Testcontainers Python](https://testcontainers.com/guides/getting-started-with-testcontainers-for-python/) ·
[Testing Trophy](https://kentcdodds.com/blog/the-testing-trophy-and-testing-classifications) ·
[Google code coverage](https://testing.googleblog.com/2020/08/code-coverage-best-practices.html)

**DevOps** — [Docker build best practices](https://docs.docker.com/build/building/best-practices/) ·
[Compose deploy spec](https://docs.docker.com/reference/compose-file/deploy/) ·
[OWASP Docker Security](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) ·
[GitHub Actions security roadmap](https://github.blog/news-insights/product-news/whats-coming-to-our-github-actions-2026-security-roadmap/) ·
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) ·
[Cloudflare Tunnel availability](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/)

**Specialized** — [OWASP GenAI/LLM](https://genai.owasp.org/) ·
[LLM guardrails](https://www.datadoghq.com/blog/llm-guardrails-best-practices/) ·
[LLM retries/circuit breakers](https://www.getmaxim.ai/articles/retries-fallbacks-and-circuit-breakers-in-llm-apps-a-production-guide/) ·
[Walk-forward validation](https://arxiv.org/html/2512.12924v1) ·
[Backtesting mistakes](https://gainium.io/blog/common-backtesting-problems) ·
[PyTorch reproducibility](https://docs.pytorch.org/docs/stable/notes/randomness.html) ·
[ML monitoring](https://www.datadoghq.com/blog/ml-model-monitoring-in-production-best-practices/)
