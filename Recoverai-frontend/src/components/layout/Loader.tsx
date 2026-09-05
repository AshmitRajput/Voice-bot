import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

/** Full-screen overlay loader — used while route data or an auth check is pending. */
export function Loader({ label = "Loading…", className }: { label?: string; className?: string }) {
  return (
    <div className={cn("fixed inset-0 z-[9999] grid place-items-center bg-background/70 backdrop-blur-sm", className)}>
      <div className="flex flex-col items-center gap-3">
        <Loader2 className="size-8 animate-spin text-primary" />
        <p className="text-sm text-muted-foreground">{label}</p>
      </div>
    </div>
  );
}

/** Inline loader for a section/card that's still fetching. */
export function InlineLoader({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" />
      {label}
    </div>
  );
}

export default Loader;
