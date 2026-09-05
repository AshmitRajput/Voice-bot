import React from "react";
import { NavLink } from "react-router-dom";

/**
 * AppShell — sidebar + content frame for every authenticated route.
 *
 * Deliberately minimal right now: only the two nav items that exist
 * (per the migration plan, step 2 is "wire App.tsx with only /dashboard
 * and /voice-test first"). Add nav items here as each route lands —
 * don't pre-build links to pages that 404.
 */

const NAV_ITEMS = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/voice-test", label: "Voice Test" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div style={styles.shell}>
      <aside style={styles.sidebar}>
        <div style={styles.brand}>
          <span style={styles.brandMark}>●</span>
          <span style={styles.brandName}>RecoverAI</span>
        </div>

        <nav style={styles.nav}>
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              style={({ isActive }) => ({
                ...styles.navLink,
                ...(isActive ? styles.navLinkActive : {}),
              })}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div style={styles.sidebarFooter}>
          <span style={{ fontFamily: "var(--font-data)", fontSize: 11 }}>v0.1.0</span>
        </div>
      </aside>

      <main style={styles.main}>{children}</main>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  shell: {
    display: "flex",
    minHeight: "100vh",
  },
  sidebar: {
    width: 208,
    flexShrink: 0,
    background: "var(--surface)",
    borderRight: "1px solid var(--border)",
    display: "flex",
    flexDirection: "column",
    padding: "20px 14px",
  },
  brand: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "0 8px 24px",
  },
  brandMark: {
    color: "var(--accent)",
    fontSize: 10,
  },
  brandName: {
    fontWeight: 600,
    fontSize: 15,
    letterSpacing: "-0.01em",
  },
  nav: {
    display: "flex",
    flexDirection: "column",
    gap: 2,
  },
  navLink: {
    display: "block",
    padding: "8px 10px",
    borderRadius: "var(--radius)",
    color: "var(--text-dim)",
    textDecoration: "none",
    fontSize: 13.5,
  },
  navLinkActive: {
    background: "var(--accent-soft)",
    color: "var(--text)",
  },
  sidebarFooter: {
    marginTop: "auto",
    padding: "8px",
    color: "var(--text-faint)",
  },
  main: {
    flex: 1,
    padding: "32px 40px",
    maxWidth: 960,
  },
};
