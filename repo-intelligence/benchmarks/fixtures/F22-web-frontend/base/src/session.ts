import { useSyncExternalStore } from "react";

type User = { name: string; roles: string[] };
let current: { user: User | null } = { user: null };
const listeners = new Set<() => void>();

export function setSession(user: User | null) {
  current = { user };
  sessionStorage.setItem("signed-in", user ? "yes" : "no");
  listeners.forEach((l) => l());
}

export function useSession() {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}
