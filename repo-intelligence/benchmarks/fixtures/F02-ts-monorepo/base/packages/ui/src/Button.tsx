import type { ReactNode } from "react";

export function Button({ children }: { children: ReactNode }) {
  return <button className="btn">{children}</button>;
}
