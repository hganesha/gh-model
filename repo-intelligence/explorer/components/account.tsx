"use client";

import { LogOut } from "lucide-react";
import { useEffect, useState } from "react";

type Me = { enabled: boolean; user: { name: string; login: string; role: "admin" | "viewer" } | null };

/** The signed-in user and a sign-out button; nothing when the Explorer runs without sign-in. */
export function Account() {
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    let live = true;
    fetch("/auth/me", { cache: "no-store" })
      .then((res) => res.json() as Promise<Me>)
      .then((body) => { if (live) setMe(body); })
      .catch(() => {});
    return () => { live = false; };
  }, []);
  if (!me?.enabled || !me.user) return null;
  const { name, login, role } = me.user;
  return (
    <div className="account">
      <span className="account-name" title={login || name}>{name}</span>
      {role === "viewer" && <span className="badge badge-neutral" title="You can look at everything but not change it">Read only</span>}
      <form method="post" action="/auth/logout">
        <button type="submit" className="icon-btn" aria-label={`Sign out ${name}`} title="Sign out">
          <LogOut size={15} aria-hidden="true" />
        </button>
      </form>
    </div>
  );
}
