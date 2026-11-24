import { authPage } from "@/lib/auth-page";

export const dynamic = "force-dynamic";

export function GET() {
  return authPage(200, "You are signed out", "Your RKP Explorer session has ended.", { href: "/auth/login", label: "Sign in again" });
}
