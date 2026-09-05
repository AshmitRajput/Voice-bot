import { useState } from "react";
import {
  PhoneCall,
  PhoneOff,
  CalendarClock,
  AlertTriangle,
  ThumbsDown,
  PhoneMissed,
  Clock,
  IndianRupee,
  Coins,
} from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent } from "@/components/ui/card";
import { KpiCard, KpiCardSkeleton } from "@/components/dashboard/KpiCard";
import { BreakdownChart } from "@/components/dashboard/BreakdownChart";
import { useDashboard } from "@/hooks/useDashboard";
import { formatCurrency } from "@/lib/utils";

const PERIODS = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
];

export default function Dashboard() {
  const [days, setDays] = useState("30");
  const { data, isLoading, isError, error } = useDashboard(Number(days));

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Recovery performance across calls, campaigns, and payments."
        actions={
          <Tabs value={days} onValueChange={setDays}>
            <TabsList>
              {PERIODS.map((p) => (
                <TabsTrigger key={p.value} value={p.value}>
                  {p.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        }
      />

      <div className="p-4 md:p-6 lg:p-8 space-y-6">
        {isError && (
          <Card className="border-destructive/40">
            <CardContent className="pt-6 text-sm text-destructive">
              Couldn't load the dashboard — {error instanceof Error ? error.message : "unknown error"}.
              Is the Django server running?
            </CardContent>
          </Card>
        )}

        {/* KPI row */}
        <div className="grid gap-4 grid-cols-2 lg:grid-cols-4">
          {isLoading ? (
            Array.from({ length: 8 }).map((_, i) => <KpiCardSkeleton key={i} />)
          ) : data ? (
            <>
              <KpiCard
                label="Calls attempted"
                value={data.totals.calls_attempted.toLocaleString("en-IN")}
                icon={PhoneCall}
                hint={`${data.totals.total_calls.toLocaleString("en-IN")} total, all time`}
              />
              <KpiCard
                label="Calls connected"
                value={data.totals.calls_connected.toLocaleString("en-IN")}
                icon={PhoneCall}
                tone="success"
                hint={`${data.totals.connection_rate}% connection rate`}
              />
              <KpiCard
                label="Callbacks"
                value={data.totals.callbacks.toLocaleString("en-IN")}
                icon={CalendarClock}
                tone="ai"
              />
              <KpiCard
                label="Complaints"
                value={data.totals.complaints.toLocaleString("en-IN")}
                icon={AlertTriangle}
                tone="destructive"
              />
              <KpiCard
                label="Declines"
                value={data.totals.declines.toLocaleString("en-IN")}
                icon={ThumbsDown}
              />
              <KpiCard
                label="Wrong numbers"
                value={data.totals.wrong_numbers.toLocaleString("en-IN")}
                icon={PhoneMissed}
              />
              <KpiCard
                label="Avg. call duration"
                value={`${Math.round(data.totals.avg_duration_seconds)}s`}
                icon={Clock}
              />
              <KpiCard
                label="Amount recovered"
                value={formatCurrency(data.recovery.amount_recovered)}
                icon={IndianRupee}
                tone="amount"
                hint="Closed cases in this period"
              />
            </>
          ) : null}
        </div>

        {/* Cost row */}
        {data && (
          <Card>
            <CardContent className="pt-6">
              <div className="flex items-center gap-2 text-sm font-medium mb-3">
                <Coins className="size-4 text-muted-foreground" />
                Provider costs this period
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-4 text-sm">
                <CostItem label="STT" value={data.costs.stt} />
                <CostItem label="TTS" value={data.costs.tts} />
                <CostItem label="LLM" value={data.costs.llm} />
                <CostItem label="Dialer" value={data.costs.dialer} />
                <CostItem label="Total" value={data.costs.total} emphasize />
              </div>
            </CardContent>
          </Card>
        )}

        {/* Breakdowns */}
        {data && (
          <div className="grid gap-4 lg:grid-cols-2">
            <BreakdownChart title="Calls by intent" data={data.by_intent} labelKey="intent" />
            <BreakdownChart title="Calls by outcome" data={data.by_outcome} labelKey="outcome" />
          </div>
        )}
      </div>
    </>
  );
}

function CostItem({ label, value, emphasize }: { label: string; value: string; emphasize?: boolean }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={emphasize ? "font-display font-semibold tabular-nums" : "tabular-nums"}>
        {formatCurrency(value)}
      </div>
    </div>
  );
}