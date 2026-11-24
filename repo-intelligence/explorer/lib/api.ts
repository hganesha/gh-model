"use client";

import { useCallback, useEffect, useState } from "react";

// All browser requests go to the same-origin proxy, which adds the credential server-side.
const PROXY = "/api/rkp/";

export class ApiError extends Error {
  status: number;
  code: string | null;
  /** Structured detail, e.g. a retracted snapshot's reason and replacement. */
  details: Record<string, unknown> | null;
  constructor(message: string, status: number, code: string | null, details: Record<string, unknown> | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

interface RequestOptions {
  body?: unknown;
  signal?: AbortSignal;
  /** Defaults to POST with a body and GET without one. */
  method?: "GET" | "POST" | "PUT" | "DELETE";
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const hasBody = options.body !== undefined;
  let res: Response;
  try {
    res = await fetch(PROXY + path, {
      method: options.method ?? (hasBody ? "POST" : "GET"),
      headers: hasBody ? { "Content-Type": "application/json" } : undefined,
      body: hasBody ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError("The explorer could not reach its server.", 0, "network");
  }
  let payload: unknown = null;
  try {
    payload = await res.json();
  } catch {
    payload = null;
  }
  if (!res.ok) {
    const p = (payload ?? {}) as { message?: unknown; detail?: unknown; code?: unknown };
    const message = typeof p.message === "string" ? p.message : typeof p.detail === "string" ? p.detail : `Request failed (${res.status})`;
    throw new ApiError(message, res.status, typeof p.code === "string" ? p.code : null, (p as { details?: Record<string, unknown> }).details ?? null);
  }
  return payload as T;
}

export function eventsUrl(jobId: string): string {
  return `${PROXY}compilations/${encodeURIComponent(jobId)}/events`;
}

/** snapshots/<hash>/... — the hash goes into the path as-is ("sha256:<hex>"). */
export function snapshotPath(hash: string, rest: string): string {
  return `snapshots/${hash}/${rest}`;
}

export interface ApiState<T> {
  data: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  reload: () => void;
}

/**
 * Fetch JSON for a path (GET) or a path + body (POST). Results are held only in
 * component state and are discarded as soon as the request key changes, so data
 * from one snapshot can never be shown under another.
 */
export function useApi<T>(path: string | null, body?: unknown): ApiState<T> {
  const bodyKey = body === undefined ? "" : JSON.stringify(body);
  const key = path === null ? null : `${path}\n${bodyKey}`;
  const [result, setResult] = useState<{ key: string; data?: T; error?: ApiError } | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (key === null || path === null) return;
    const controller = new AbortController();
    api<T>(path, { body: bodyKey ? JSON.parse(bodyKey) : undefined, signal: controller.signal }).then(
      (data) => setResult({ key, data }),
      (err: unknown) => {
        if (controller.signal.aborted) return;
        const error = err instanceof ApiError ? err : new ApiError("Unexpected error", 0, null);
        setResult({ key, error });
      },
    );
    return () => controller.abort();
    // bodyKey is part of key
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce]);

  const reload = useCallback(() => {
    setResult(null);
    setNonce((n) => n + 1);
  }, []);

  const current = result && result.key === key ? result : null;
  return { data: current?.data, error: current?.error, loading: key !== null && current === null, reload };
}
