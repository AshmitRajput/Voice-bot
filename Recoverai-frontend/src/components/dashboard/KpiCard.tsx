import type { LucideIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export function KpiCard({
  label,
  value,
  icon: Icon,
  tone = "default",
  hint,
}: {
  label: string;
  value: string;
  icon: LucideIcon;
  tone?: "default" | "success" | "destructive" | "ai" | "amount";
  hint?: string;
}) {
  const toneClasses: Record<string, string> = {
    default: "bg-primary/10 text-primary",
    success: "bg-[color:var(--success)]/12 text-[color:var(--success)]",
    destructive: "bg-destructive/10 text-destructive",
    ai: "bg-[color:var(--ai)]/12 text-[color:var(--ai)]",
    amount: "bg-[color:var(--amount)]/12 text-[color:var(--amount)]",
  };

  return (
    <Card>
      <CardContent className="pt-6">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className="mt-1 text-2xl font-display font-semibold tracking-tight tabular-nums truncate">
              {value}
            </div>
            {hint && <div className="mt-1 text-[11px] text-muted-foreground">{hint}</div>}
          </div>
          <div className={cn("shrink-0 rounded-md p-2", toneClasses[tone])}>
            <Icon className="size-4" />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function KpiCardSkeleton() {
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="h-3 w-20 rounded bg-muted animate-pulse" />
        <div className="mt-2 h-7 w-16 rounded bg-muted animate-pulse" />
      </CardContent>
    </Card>
  );
}