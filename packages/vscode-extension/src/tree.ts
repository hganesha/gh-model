import * as vscode from "vscode";
import type { Assertion, Bundle, Conflict, Entity, Unknown } from "@rkp/bundle";

/** Kinds shown as top-level groups, in this order; any other kind goes under "Other kinds". */
const PRIMARY_KINDS: [string, string][] = [
  ["Component", "Components"],
  ["Endpoint", "Endpoints"],
  ["Table", "Tables"],
  ["EventTopic", "Event topics"],
  ["DeploymentUnit", "Deployment units"],
  ["AIComponent", "AI components"],
  ["Job", "Jobs"],
  ["Program", "Programs"],
  ["ArchitecturePattern", "Architecture patterns"],
  ["Antipattern", "Antipatterns"],
];

export type Node =
  | { type: "overview" }
  | { type: "fact"; label: string; value: string }
  | { type: "kind"; kind: string; label: string }
  | { type: "otherKinds" }
  | { type: "entity"; entity: Entity }
  | { type: "relation"; assertion: Assertion; direction: "out" | "in" }
  | { type: "unknowns" }
  | { type: "unknown"; unknown: Unknown }
  | { type: "conflicts" }
  | { type: "conflict"; conflict: Conflict };

export class KnowledgeTree implements vscode.TreeDataProvider<Node> {
  private readonly changed = new vscode.EventEmitter<Node | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  private bundle: Bundle | undefined;
  private status = "";

  update(bundle: Bundle | undefined, status: string): void {
    this.bundle = bundle;
    this.status = status;
    this.changed.fire(undefined);
  }

  getChildren(node?: Node): Node[] {
    const bundle = this.bundle;
    if (!bundle) return [];
    if (!node) {
      const roots: Node[] = [{ type: "overview" }];
      for (const [kind, label] of PRIMARY_KINDS) if (bundle.ofKind(kind).length) roots.push({ type: "kind", kind, label });
      if (otherKinds(bundle).length) roots.push({ type: "otherKinds" });
      roots.push({ type: "conflicts" }, { type: "unknowns" });
      return roots;
    }
    switch (node.type) {
      case "overview": {
        const m = bundle.manifest;
        return [
          { type: "fact", label: "Status", value: this.status },
          { type: "fact", label: "Repository", value: m.repository_id },
          { type: "fact", label: "Commit", value: m.commit_sha.slice(0, 12) },
          { type: "fact", label: "Snapshot", value: m.status },
          { type: "fact", label: "Engine", value: `${m.engine.name} ${m.engine.version}` },
          { type: "fact", label: "Files read", value: String(m.counts.files ?? 0) },
        ];
      }
      case "kind":
        return bundle.ofKind(node.kind).map((entity) => ({ type: "entity", entity }));
      case "otherKinds":
        return otherKinds(bundle).map((kind) => ({ type: "kind", kind, label: kind }));
      case "entity": {
        const id = node.entity.id;
        return [
          ...(bundle.outgoing.get(id) ?? []).map((assertion): Node => ({ type: "relation", assertion, direction: "out" })),
          ...(bundle.incoming.get(id) ?? []).map((assertion): Node => ({ type: "relation", assertion, direction: "in" })),
        ];
      }
      case "unknowns":
        return bundle.allUnknowns().map((unknown) => ({ type: "unknown", unknown }));
      case "conflicts":
        return bundle.conflicts.map((conflict) => ({ type: "conflict", conflict }));
      default:
        return [];
    }
  }

  getTreeItem(node: Node): vscode.TreeItem {
    const bundle = this.bundle!;
    const None = vscode.TreeItemCollapsibleState.None;
    const Collapsed = vscode.TreeItemCollapsibleState.Collapsed;
    switch (node.type) {
      case "overview": {
        const item = new vscode.TreeItem("Overview", vscode.TreeItemCollapsibleState.Expanded);
        item.iconPath = new vscode.ThemeIcon("info");
        return item;
      }
      case "fact": {
        const item = new vscode.TreeItem(node.label, None);
        item.description = node.value;
        item.tooltip = `${node.label}: ${node.value}`;
        return item;
      }
      case "kind": {
        const item = new vscode.TreeItem(node.label, Collapsed);
        item.description = String(bundle.ofKind(node.kind).length);
        item.iconPath = new vscode.ThemeIcon("symbol-class");
        return item;
      }
      case "otherKinds": {
        const item = new vscode.TreeItem("Other kinds", Collapsed);
        item.description = String(otherKinds(bundle).length);
        item.iconPath = new vscode.ThemeIcon("folder");
        return item;
      }
      case "entity": {
        const { entity } = node;
        const hasRelations = (bundle.outgoing.get(entity.id)?.length ?? 0) + (bundle.incoming.get(entity.id)?.length ?? 0) > 0;
        const item = new vscode.TreeItem(entity.name, hasRelations ? Collapsed : None);
        item.description = describeEntity(entity);
        item.tooltip = new vscode.MarkdownString(
          [`**${entity.name}** · ${entity.kind}`, "", "```json", JSON.stringify(entity.attributes, null, 2), "```", "", `\`${entity.id}\``].join("\n"),
        );
        item.iconPath = new vscode.ThemeIcon("symbol-object");
        item.command = evidenceCommand(entity.evidence_ids);
        return item;
      }
      case "relation": {
        const { assertion, direction } = node;
        const other = direction === "out" ? assertion.object : assertion.subject;
        const item = new vscode.TreeItem(direction === "out" ? `${assertion.predicate} → ${bundle.label(other)}` : `← ${assertion.predicate} ${bundle.label(other)}`, None);
        item.description = `${assertion.evidence_class} · ${assertion.epistemic_status}${assertion.resolution === "candidate" ? " · candidate" : ""}`;
        item.tooltip = `${assertion.assertion_kind}, ${assertion.evidence_ids.length} evidence`;
        item.iconPath = new vscode.ThemeIcon(direction === "out" ? "arrow-right" : "arrow-left");
        item.command = evidenceCommand(assertion.evidence_ids);
        return item;
      }
      case "unknowns": {
        const count = bundle.allUnknowns().length;
        const item = new vscode.TreeItem("Unknowns", count ? Collapsed : None);
        item.description = String(count);
        item.iconPath = new vscode.ThemeIcon("question");
        return item;
      }
      case "unknown": {
        const { unknown } = node;
        const item = new vscode.TreeItem(`${unknown.predicate}: ${bundle.label(unknown.subject)}`, None);
        item.description = unknown.reason;
        item.tooltip = `${unknown.status}: ${unknown.reason}`;
        const subject = bundle.entities.get(unknown.subject);
        if (subject) item.command = evidenceCommand(subject.evidence_ids);
        return item;
      }
      case "conflicts": {
        const item = new vscode.TreeItem("Conflicts", bundle.conflicts.length ? Collapsed : None);
        item.description = String(bundle.conflicts.length);
        item.iconPath = new vscode.ThemeIcon("warning");
        return item;
      }
      case "conflict": {
        const { conflict } = node;
        const item = new vscode.TreeItem(conflict.conflict_type.replace(/_/g, " "), None);
        item.description = conflict.basis;
        item.tooltip = conflict.subject_ids.map((id) => bundle.label(id)).join(", ");
        item.command = evidenceCommand(conflict.evidence_ids);
        return item;
      }
    }
  }
}

function otherKinds(bundle: Bundle): string[] {
  const primary = new Set(PRIMARY_KINDS.map(([kind]) => kind));
  return bundle.kinds().filter((kind) => !primary.has(kind));
}

function describeEntity(entity: Entity): string {
  const a = entity.attributes;
  const parts = [a.component_kind, a.method, a.path, a.primary_language].filter((v): v is string => typeof v === "string" && v !== "");
  return parts.join(" · ");
}

function evidenceCommand(evidenceIds: readonly string[]): vscode.Command | undefined {
  return evidenceIds.length ? { command: "rkp.openEvidence", title: "Open Evidence", arguments: [[...evidenceIds]] } : undefined;
}
