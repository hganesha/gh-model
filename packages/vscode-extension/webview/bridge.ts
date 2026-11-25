/**
 * The Explorer's components call `fetch("/api/rkp/<path>")`. In the webview there is no
 * server: this bridge answers those calls by posting them to the extension, which
 * forwards them to the engine (`rkp serve --stdio`) and posts the answer back.
 * Every other URL fails, as the webview's content security policy would make it.
 */

export interface VsCodeApi {
  postMessage(message: unknown): void;
}

declare function acquireVsCodeApi(): VsCodeApi;

export type FromExtension =
  | { type: "response"; id: number; status: number; body: unknown }
  | { type: "reload" };

const PREFIX = "/api/rkp/";

export const vscode: VsCodeApi = acquireVsCodeApi();

const pending = new Map<number, (reply: { status: number; body: unknown }) => void>();
const reloadListeners = new Set<() => void>();
let next = 1;

window.addEventListener("message", (event: MessageEvent<FromExtension>) => {
  const message = event.data;
  if (message?.type === "response") {
    pending.get(message.id)?.({ status: message.status, body: message.body });
    pending.delete(message.id);
  } else if (message?.type === "reload") {
    for (const listener of reloadListeners) listener();
  }
});

/** Called when the bundle on disk changed and every view should read it again. */
export function onReload(listener: () => void): () => void {
  reloadListeners.add(listener);
  return () => reloadListeners.delete(listener);
}

function abortError(): DOMException {
  return new DOMException("The request was aborted.", "AbortError");
}

export function request(method: string, path: string, body: unknown, signal?: AbortSignal | null): Promise<{ status: number; body: unknown }> {
  if (signal?.aborted) return Promise.reject(abortError());
  const id = next++;
  return new Promise((resolve, reject) => {
    pending.set(id, resolve);
    signal?.addEventListener("abort", () => {
      pending.delete(id);
      reject(abortError());
    });
    vscode.postMessage({ type: "request", id, method, path, body });
  });
}

export function installFetchBridge(): void {
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.pathname + input.search : input.url;
    const path = url.startsWith(PREFIX) ? url.slice(PREFIX.length) : null;
    if (path === null) throw new TypeError(`The webview has no network access (${url}).`);
    const text = typeof init?.body === "string" ? init.body : undefined;
    const reply = await request(init?.method ?? "GET", path, text === undefined ? undefined : JSON.parse(text), init?.signal);
    return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { "Content-Type": "application/json" } });
  };
}
