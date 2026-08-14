// API base + helpers. The backend port is injected at container start via NEXT_PUBLIC_API_URL.

// `??` (not `||`) so an explicit empty string means "same origin" (production behind the Caddy
// reverse proxy AND the dev stack behind its compose `proxy` service: the browser calls relative
// `/api/...`). Unset (bare local `next dev`) falls back to :8000.
export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// The session contract REQUIRES a same-origin deployment. `same-origin` is fetch's default, but
// it is spelled out on every call so the dependency is visible: the __Host-rh_sid cookie flows
// only when page and API share an origin, because the backend deliberately ships CORS with
// allow_credentials=false (docs/AUTH_THREAT_MODEL.md §5.9). Do NOT "fix" a cross-origin
// API_URL by switching this to "include" — that needs credentialed CORS server-side, which is
// exactly the posture §5.9 rejects; make the deployment same-origin instead (as prod's Caddy and
// dev's compose proxy both do).
export const CREDENTIALS: RequestCredentials = "same-origin";

export async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { cache: "no-store", credentials: CREDENTIALS });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`${res.status} ${path}: ${detail.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

export async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: CREDENTIALS,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`${res.status} ${path}: ${detail.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

export const fetcher = <T>(path: string) => getJSON<T>(path);

/**
 * Stream a POST SSE endpoint. Calls `onEvent` for each parsed `data:` JSON object.
 * Returns when the stream ends. Throws on a non-OK initial response.
 */
export async function streamSSE(
  path: string,
  body: unknown,
  onEvent: (event: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: CREDENTIALS,
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    throw new Error(`${res.status} ${path}: ${detail.slice(0, 200)}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE events are separated by a blank line.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data:")) {
          const json = line.slice(5).trim();
          if (json) {
            try {
              onEvent(JSON.parse(json));
            } catch {
              /* ignore partial / malformed frames */
            }
          }
        }
      }
    }
  }
}
