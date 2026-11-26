"use client";

import { AlertTriangle, Boxes, Braces, CalendarClock, Database, FileText, FlaskConical, KeyRound, Loader2, PanelsTopLeft, Rocket, Scale, ShieldCheck, Sparkles, Workflow, X } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import type { ApiError } from "@/lib/api";
import { measured, percent, percentLabel } from "@/lib/format";
import type { Assertion, Ratio } from "@/lib/types";

const PLANE_ICONS = {
  architecture: Boxes,
  contracts: Braces,
  data: Database,
  jobs: CalendarClock,
  ai: Sparkles,
  tests: FlaskConical,
  deployment: Rocket,
  experience: PanelsTopLeft,
  business: Scale,
  security: KeyRound,
  quality: ShieldCheck,
  purpose: Workflow,
  files: FileText,
} as const;

export function PlaneIcon({ plane, size = 16 }: { plane: string; size?: number }) {
  const Icon = PLANE_ICONS[plane as keyof typeof PLANE_ICONS] ?? FileText;
  return <Icon size={size} strokeWidth={1.7} aria-hidden="true" />;
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state state-loading" role="status">
      <Loader2 size={15} className="spin" aria-hidden="true" />
      <span>{label}…</span>
    </div>
  );
}

export function ErrorNote({ error, onRetry, what }: { error: ApiError | { message: string }; onRetry?: () => void; what?: string }) {
  return (
    <div className="state state-error" role="alert">
      <AlertTriangle size={15} aria-hidden="true" />
      <div>
        <strong>{what ? `Could not load ${what}.` : "Something went wrong."}</strong>
        <p>{error.message}</p>
      </div>
      {onRetry && (
        <button type="button" className="btn btn-quiet btn-small" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function Note({ children, tone = "plain" }: { children: ReactNode; tone?: "plain" | "warn" }) {
  return <p className={`note note-${tone}`}>{children}</p>;
}

/** "n of m" with a thin bar; says "not measured" when there was nothing to measure. */
export function Coverage({ label, ratio, compact = false }: { label?: string; ratio: Ratio | null | undefined; compact?: boolean }) {
  const ok = measured(ratio);
  return (
    <div className={`coverage ${compact ? "coverage-compact" : ""}`}>
      <div className="coverage-line">
        {label && <span className="coverage-label">{label}</span>}
        {ok ? (
          <span className="coverage-figure">
            <b>{ratio.numerator.toLocaleString("en-US")}</b> of {ratio.denominator.toLocaleString("en-US")}
            <i>{percentLabel(ratio)}</i>
          </span>
        ) : (
          <span className="coverage-figure coverage-unmeasured">not measured</span>
        )}
      </div>
      <Bar ratio={ok ? ratio : null} />
    </div>
  );
}

export function Bar({ ratio }: { ratio: Ratio | null }) {
  if (!ratio) return <div className="bar bar-unmeasured" aria-hidden="true" />;
  const p = percent(ratio);
  const tone = p >= 90 ? "good" : p >= 50 ? "mid" : "low";
  return (
    <div className={`bar bar-${tone}`} role="img" aria-label={`${ratio.numerator} of ${ratio.denominator}`}>
      <span style={{ width: `${Math.max(p, ratio.numerator > 0 ? 2 : 0)}%` }} />
    </div>
  );
}

export function Badge({ children, tone = "neutral", title }: { children: ReactNode; tone?: string; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

/** Whether a fact is a candidate or inferred rather than exact and supported. */
export function isTentative(a: Pick<Assertion, "resolution" | "epistemic_status">): boolean {
  return a.resolution === "candidate" || a.epistemic_status === "inferred";
}

export function AssertionBadges({ assertion }: { assertion: Assertion }) {
  const status = assertion.epistemic_status || "unknown";
  return (
    <span className="badges">
      <Badge tone={status === "verified" ? "verified" : status === "supported" ? "supported" : "inferred"}>{status}</Badge>
      {assertion.evidence_class && (
        <Badge tone="class" title={`Evidence class ${assertion.evidence_class}`}>
          {assertion.evidence_class}
        </Badge>
      )}
      {assertion.resolution === "candidate" && <Badge tone="candidate">candidate</Badge>}
    </span>
  );
}

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/** Modal dialog with a focus trap. Escape is handled by the shell so layers close in order. */
export function Modal({
  title,
  onClose,
  children,
  className = "",
  hideTitle = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  hideTitle?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = panel.current;
    const preferred = node?.querySelector<HTMLElement>("[data-autofocus]") ?? node?.querySelector<HTMLElement>(FOCUSABLE);
    (preferred ?? node)?.focus();
    return () => previous?.focus?.();
  }, []);

  function trap(e: React.KeyboardEvent) {
    if (e.key !== "Tab" || !panel.current) return;
    const items = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="modal-layer" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={panel} className={`modal ${className}`} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} onKeyDown={trap}>
        {!hideTitle && (
          <header className="modal-head">
            <h2>{title}</h2>
            <button type="button" className="icon-btn" onClick={onClose} aria-label="Close dialog">
              <X size={16} aria-hidden="true" />
            </button>
          </header>
        )}
        {children}
      </div>
    </div>
  );
}
