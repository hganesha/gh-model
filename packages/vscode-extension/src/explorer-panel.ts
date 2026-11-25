import { randomBytes } from "node:crypto";
import * as vscode from "vscode";
import { EngineServer } from "./engine";

/** What the webview may ask for. Anything else is ignored. */
type FromWebview =
  | { type: "request"; id: number; method: string; path: string; body?: unknown }
  | { type: "openEntity"; id: string }
  | { type: "analyze" };

const METHODS = new Set(["GET", "POST", "PUT"]);

/**
 * The Explorer's views in a webview panel. The page has no network access: its API
 * calls come here as messages and go to `rkp serve --stdio` (EngineServer).
 */
export class ExplorerPanel implements vscode.Disposable {
  static readonly viewType = "rkp.explorer";
  private static current: ExplorerPanel | undefined;
  /** Requests answered for any panel's page; the integration tests read it to see the page run. */
  static served: string[] = [];
  private readonly disposables: vscode.Disposable[] = [];

  static show(context: vscode.ExtensionContext, server: EngineServer, openEntity: (id: string) => void, analyze: () => void): ExplorerPanel {
    if (ExplorerPanel.current) {
      ExplorerPanel.current.panel.reveal();
      return ExplorerPanel.current;
    }
    const root = vscode.Uri.joinPath(context.extensionUri, "dist", "webview");
    const panel = vscode.window.createWebviewPanel(ExplorerPanel.viewType, "Repository Explorer", vscode.ViewColumn.Active, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [root],
    });
    panel.iconPath = vscode.Uri.joinPath(context.extensionUri, "media", "icon.svg");
    ExplorerPanel.current = new ExplorerPanel(panel, root, server, openEntity, analyze);
    return ExplorerPanel.current;
  }

  /** Tell an open panel that the bundle on disk changed. */
  static reload(): void {
    void ExplorerPanel.current?.panel.webview.postMessage({ type: "reload" });
  }

  static isOpen(): boolean {
    return ExplorerPanel.current !== undefined;
  }

  private constructor(
    readonly panel: vscode.WebviewPanel,
    root: vscode.Uri,
    private readonly server: EngineServer,
    openEntity: (id: string) => void,
    analyze: () => void,
  ) {
    panel.webview.html = html(panel.webview, root);
    panel.webview.onDidReceiveMessage(
      async (message: FromWebview) => {
        if (message?.type === "request" && typeof message.id === "number" && typeof message.path === "string" && METHODS.has(message.method)) {
          const reply = await this.server.request(message.method, message.path, message.body);
          ExplorerPanel.served.push(`${message.method} ${message.path.split("?")[0]} ${reply.status}`);
          void panel.webview.postMessage({ type: "response", id: message.id, ...reply });
        } else if (message?.type === "openEntity" && typeof message.id === "string") {
          openEntity(message.id);
        } else if (message?.type === "analyze") {
          analyze();
        }
      },
      undefined,
      this.disposables,
    );
    panel.onDidDispose(() => this.dispose(), undefined, this.disposables);
  }

  dispose(): void {
    ExplorerPanel.current = undefined;
    for (const d of this.disposables.splice(0)) d.dispose();
    this.panel.dispose();
  }
}

function html(webview: vscode.Webview, root: vscode.Uri): string {
  const nonce = randomBytes(16).toString("base64");
  const script = webview.asWebviewUri(vscode.Uri.joinPath(root, "main.js"));
  const style = webview.asWebviewUri(vscode.Uri.joinPath(root, "main.css"));
  // No connect-src: the page cannot reach any server, including the engine's.
  const csp = [
    "default-src 'none'",
    `script-src 'nonce-${nonce}'`,
    `style-src ${webview.cspSource} 'unsafe-inline'`,
    `font-src ${webview.cspSource}`,
    `img-src ${webview.cspSource} data:`,
  ].join("; ");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${style}">
<title>Repository Explorer</title>
</head>
<body>
<div id="root"></div>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
}
