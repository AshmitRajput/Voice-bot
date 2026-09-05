import type { ReactNode } from "react";
import { Loader2, AlertTriangle, Inbox } from "lucide-react";
import { cn } from "@/lib/utils";

/** Full-width loading row for a table/list body. */
export function LoadingRow({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" />
      {label}
    </div>
  );
}

/** Error row — consistent phrasing + the endpoint that failed, for fast debugging. */
export function ErrorRow({ endpoint, message }: { endpoint?: string; message?: string }) {
  return (
    <div className="flex flex-col items-center gap-1.5 py-12 text-center">
      <AlertTriangle className="size-5 text-destructive" />
      <p className="text-sm text-destructive">
        {message ?? "Couldn't load this data."}
      </p>
      {endpoint && (
        <code className="text-xs rounded bg-muted px-1.5 py-0.5 text-muted-foreground">{endpoint}</code>
      )}
    </div>
  );
}

/** Empty-result row — same visual weight as LoadingRow/ErrorRow, not a full-page block. */
export function EmptyRow({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-1.5 py-12 text-center text-sm text-muted-foreground">
      <Inbox className="size-5 opacity-50" />
      {children}
    </div>
  );
}

/** Same three, but for a page with no table shell to sit inside of — Dashboard, Personas, etc. */
export function StatePanel({
  variant, endpoint, message, children, className,
}: {
  variant: "loading" | "error" | "empty";
  endpoint?: string;
  message?: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-lg border border-dashed p-10", className)}>
      {variant === "loading" && <LoadingRow label={message} />}
      {variant === "error" && <ErrorRow endpoint={endpoint} message={message} />}
      {variant === "empty" && <EmptyRow>{children ?? message ?? "Nothing here yet."}</EmptyRow>}
    </div>
  );
}
