"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useAuth } from "@/components/auth-provider";
import styles from "./app-shell.module.css";

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const pathname = usePathname();
  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar} aria-label="Application navigation and account">
        <Link href="/" className={styles.brand} aria-label="RescueDesk home">
          <span className={styles.mark}>R</span>
          <span>
            <strong>RescueDesk</strong>
            <small>Contract exit control</small>
          </span>
        </Link>
        <nav className={styles.nav} aria-label="Primary navigation">
          <Link href="/" className={pathname === "/" ? styles.active : ""}>
            Matters
          </Link>
        </nav>
        <div className={styles.disclaimer}>
          <strong>Demonstration only</strong>
          <span>Sample data only. Not legal or financial advice.</span>
        </div>
        <div className={styles.account}>
          <span className={styles.avatar}>{user?.display_name.slice(0, 1).toUpperCase()}</span>
          <span className={styles.accountText}>
            <strong>{user?.display_name}</strong>
            <small>{user?.role}</small>
          </span>
          <button type="button" className={styles.signOut} onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>
      <main className={styles.main}>{children}</main>
    </div>
  );
}
