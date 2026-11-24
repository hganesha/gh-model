import "server-only";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

// Recorded API responses, so the Explorer can be built, tested and demonstrated without the API
// (GI-030). Recording: RKP_API_RECORD=<dir> makes the proxy save every JSON answer it forwards.
// Replay: RKP_API_REPLAY=<dir> makes it answer only from those files and never call the API;
// a request that was not recorded gets 404, so a replaying Explorer can never change anything.

export type Recorded = { method: string; path: string; status: number; body: string };

export const recordDir = () => process.env.RKP_API_RECORD?.trim() || null;
export const replayDir = () => process.env.RKP_API_REPLAY?.trim() || null;

/** One file per request: method, API path with query, and the JSON body for POST reads. */
export function replayKey(method: string, apiPath: string, body: string | undefined): string {
  let canonical = "";
  if (body) {
    try {
      canonical = JSON.stringify(sortKeys(JSON.parse(body)));
    } catch {
      canonical = body;
    }
  }
  return createHash("sha256").update(`${method}\n${apiPath}\n${canonical}`).digest("hex").slice(0, 40);
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((k) => [k, sortKeys((value as Record<string, unknown>)[k])]));
  }
  return value;
}

export async function record(dir: string, method: string, apiPath: string, body: string | undefined, status: number, text: string) {
  if (status >= 500) return;
  await mkdir(dir, { recursive: true });
  const entry: Recorded = { method, path: apiPath, status, body: text };
  await writeFile(path.join(dir, `${replayKey(method, apiPath, body)}.json`), JSON.stringify(entry, null, 1) + "\n");
}

export async function replay(dir: string, method: string, apiPath: string, body: string | undefined): Promise<Recorded | null> {
  try {
    return JSON.parse(await readFile(path.join(dir, `${replayKey(method, apiPath, body)}.json`), "utf8")) as Recorded;
  } catch {
    return null;
  }
}
