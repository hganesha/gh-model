import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useSession } from "../session";

export default function RequireRole({ role, children }: { role: string; children: ReactNode }) {
  const session = useSession();
  if (!session.user) {
    return <Navigate to="/login" replace />;
  }
  if (!session.user.roles.includes(role)) {
    return <p>You do not have access to this page.</p>;
  }
  return <>{children}</>;
}
