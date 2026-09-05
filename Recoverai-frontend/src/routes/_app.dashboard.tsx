import React, { useEffect, useState } from "react";
import { apiGet } from "@/lib/api";

/**
 * Dashboard — three real endpoints from views_admin.py, fetched in
 * parallel so one failing doesn't blank the whole page:
 *
 *   GET /api/admin/recovery/dashboard/?days=30
 *       -> { success, period_days, totals, by_intent, by_outcome, costs, recovery }
 *   GET /api/admin/customers/?page_size=1
 *       -> DRF PageNumberPagination: { count, next, previous, results }
 *       (only `count` is used here -- cheap total-customers number)
 *   GET /api/admin/recovery/cases/?status=open
 *       -> { success, count, cases }
 *       (only `count` is used here -- open-case number)
 */

interface DashboardTotals {
  total_calls: number;
  calls_attempted: number;
  calls_connected: number;
  connection_rate: number;
  complaints: number;
  callbacks: number;
  declines: number;
  wrong_numbers: number;
  avg_duration_seconds: number;
}

interface DashboardResponse {
  success: boolean;
  period_days: number;
  totals: DashboardTotals;
  by_intent: { intent: string; count: number }[];
  by_outcome: { outcome: string; count: number }[];
  costs: { stt: string; tts: string; llm: string; dialer: string; total: string };
  recovery: { amount_recovered: string };
}

interface PaginatedResponse<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

interface CasesCountResponse {
  success: boolean;
  count: number;
}

type LoadState = "loading" | "ready" | "error";

export default function Dashboard() {
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [totalCustomers, setTotalCustomers] = useState<number | null>(null);
  const [openCases, setOpenCases] = useState<number | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [errorMsg, setErrorMsg] = useState("");

  useEffect(() => {
    let cancelled = false;

    Promise.allSettled([
      apiGet<DashboardResponse>("/api/admin/recovery/dashboard/?days=30"),
      apiGet<PaginatedResponse<unknown>>("/api/admin/customers/?page_size=1"),
      apiGet<CasesCountResponse>("/api/admin/recovery/cases/?status=open"),
    ]).then(([dashRes, custRes, casesRes]) => {
      if (cancelled) return;

      if (dashRes.status === "fulfilled") {
        setDashboard(dashRes.value);
      } else {
        setErrorMsg(String(dashRes.reason?.message || dashRes.reason));
      }
      if (custRes.status === "fulfilled") setTotalCustomers(custRes.value.count);
      if (casesRes.status === "fulfilled") setOpenCases(casesRes.value.count);

      setState(dashRes.status === "fulfilled" ? "ready" : "error");
    });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div>
      <h1 style={styles.h1}>Dashboard</h1>
      <p style={styles.sub}>Recovery activity, last 30 days.</p>

      {state === "loading" && <p style={{ color: "var(--text-dim)" }}>Loading…</p>}

      {state === "error" && (
        <div style={styles.errorBox}>
          <strong>Couldn't reach the dashboard endpoint.</strong>
          <p style={{ margin: "6px 0 0", color: "var(--text-dim)" }}>{errorMsg}</p>
          <p style={{ margin: "6px 0 0", color: "var(--text-dim)" }}>
            Check that <code style={styles.code}>python manage.py runserver</code> is running
            and <code style={styles.code}>/api/admin/recovery/dashboard/</code> exists.
          </p>
        </div>
      )}

      {state === "ready" && dashboard && (
        <>
          <div style={styles.grid}>
            <Kpi label="Open cases" value={openCases} />
            <Kpi label="Customers" value={totalCustomers} />
            <Kpi
              label="Amount recovered"
              value={`₹${dashboard.recovery.amount_recovered}`}
              mono
            />
            <Kpi label="Calls (30d)" value={dashboard.totals.calls_attempted} />
            <Kpi
              label="Connection rate"
              value={`${dashboard.totals.connection_rate}%`}
              mono
            />
            <Kpi
              label="Avg call length"
              value={`${Math.round(dashboard.totals.avg_duration_seconds)}s`}
              mono
            />
            <Kpi label="Callbacks" value={dashboard.totals.callbacks} />
            <Kpi label="Complaints" value={dashboard.totals.complaints} />
            <Kpi label="Declines" value={dashboard.totals.declines} />
            <Kpi label="Wrong numbers" value={dashboard.totals.wrong_numbers} />
            <Kpi label="Total cost" value={`₹${dashboard.costs.total}`} mono />
          </div>

          <div style={styles.breakdownRow}>
            <Breakdown
              title="By intent"
              rows={dashboard.by_intent.map((r) => ({ label: r.intent, count: r.count }))}
            />
            <Breakdown
              title="By outcome"
              rows={dashboard.by_outcome.map((r) => ({ label: r.outcome, count: r.count }))}
            />
          </div>
        </>
      )}
    </div>
  );
}

function Kpi({ label, value, mono }: { label: string; value: unknown; mono?: boolean }) {
  return (
    <div style={styles.card}>
      <div style={styles.cardLabel}>{label}</div>
      <div style={{ ...styles.cardValue, fontFamily: mono ? "var(--font-data)" : "var(--font-ui)" }}>
        {value === undefined || value === null ? "—" : String(value)}
      </div>
    </div>
  );
}

function Breakdown({ title, rows }: { title: string; rows: { label: string; count: number }[] }) {
  return (
    <div style={styles.breakdownCard}>
      <div style={styles.breakdownTitle}>{title}</div>
      {rows.length === 0 && <p style={{ color: "var(--text-faint)", fontSize: 13 }}>No data yet.</p>}
      {rows.map((r) => (
        <div key={r.label} style={styles.breakdownRowLine}>
          <span>{r.label}</span>
          <span style={{ fontFamily: "var(--font-data)", color: "var(--text-dim)" }}>{r.count}</span>
        </div>
      ))}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  h1: {
    fontSize: 22,
    fontWeight: 600,
    letterSpacing: "-0.01em",
    margin: "0 0 4px",
  },
  sub: {
    color: "var(--text-dim)",
    margin: "0 0 28px",
    fontSize: 13.5,
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))",
    gap: 12,
    marginBottom: 28,
  },
  card: {
    background: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    padding: "16px 18px",
  },
  cardLabel: {
    color: "var(--text-dim)",
    fontSize: 12.5,
    marginBottom: 8,
  },
  cardValue: {
    fontSize: 20,
    fontWeight: 600,
  },
  breakdownRow: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 12,
  },
  breakdownCard: {
    background: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    padding: "16px 18px",
  },
  breakdownTitle: {
    fontSize: 13,
    fontWeight: 600,
    marginBottom: 10,
  },
  breakdownRowLine: {
    display: "flex",
    justifyContent: "space-between",
    padding: "5px 0",
    fontSize: 13,
    borderBottom: "1px solid var(--border)",
  },
  errorBox: {
    background: "var(--danger-soft)",
    border: "1px solid var(--danger)",
    borderRadius: "var(--radius)",
    padding: "14px 16px",
    maxWidth: 480,
  },
  code: {
    background: "var(--surface-raised)",
    padding: "1px 5px",
    borderRadius: 4,
    fontFamily: "var(--font-data)",
    fontSize: 12.5,
  },
};
