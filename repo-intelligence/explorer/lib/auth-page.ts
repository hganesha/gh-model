import "server-only";

const escape = (s: string) => s.replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);

/** A small self-contained page for sign-in outcomes; it loads nothing and runs no script. */
export function authPage(status: number, title: string, message: string, action?: { href: string; label: string }): Response {
  const link = action ? `<p><a href="${escape(action.href)}">${escape(action.label)}</a></p>` : "";
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${escape(title)} · RKP Explorer</title>
<style>:root{color-scheme:light dark;--ink:#293129;--muted:#697263;--paper:#f7f8f4;--accent:#3f6b47}@media (prefers-color-scheme:dark){:root{--ink:#e4eadf;--muted:#9aa594;--paper:#151a16;--accent:#8fc39a}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,sans-serif;padding:16px}
main{max-width:420px}h1{font-size:20px;margin:0 0 8px}p{color:var(--muted);margin:0 0 16px}a{color:var(--accent);font-weight:600}</style></head>
<body><main><h1>${escape(title)}</h1><p>${escape(message)}</p>${link}</main></body></html>`;
  return new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'", "X-Content-Type-Options": "nosniff" } });
}
