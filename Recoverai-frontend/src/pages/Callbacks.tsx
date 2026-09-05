import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/AppShell";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCallbacks } from "@/hooks/useRecoveryOps";

const STATUS_TONE: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  requested: "outline",
  confirmed: "default",
  completed: "secondary",
  missed: "destructive",
};

export default function Callbacks() {
  const [status, setStatus] = useState<string | undefined>(undefined);
  const navigate = useNavigate();
  const { data, isLoading, isError } = useCallbacks(status);

  return (
    <div className="space-y-6">
      <PageHeader title="Callbacks" description={data ? `${data.count} total` : undefined} />

      <Select value={status ?? "all"} onValueChange={(v) => setStatus(v === "all" ? undefined : v)}>
        <SelectTrigger className="w-44">
          <SelectValue placeholder="Filter by status" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All statuses</SelectItem>
          <SelectItem value="requested">Requested</SelectItem>
          <SelectItem value="confirmed">Confirmed</SelectItem>
          <SelectItem value="completed">Completed</SelectItem>
          <SelectItem value="missed">Missed</SelectItem>
        </SelectContent>
      </Select>

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load callbacks. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/admin/recovery/callbacks/</code>.
        </p>
      )}

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Customer</TableHead>
                <TableHead>Case</TableHead>
                <TableHead>Scheduled for</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.callbacks.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground py-8">
                    No callbacks match that filter.
                  </TableCell>
                </TableRow>
              )}
              {data?.callbacks.map((cb) => (
                <TableRow
                  key={cb.id}
                  className="cursor-pointer"
                  onClick={() => navigate(`/customers/${cb.customer_id}`)}
                >
                  <TableCell className="font-mono text-sm">#{cb.customer_id}</TableCell>
                  <TableCell className="font-mono text-sm text-muted-foreground">
                    #{cb.recovery_case_id}
                  </TableCell>
                  <TableCell className="text-sm">{cb.scheduled_for ?? "—"}</TableCell>
                  <TableCell className="text-muted-foreground">{cb.reason || "—"}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_TONE[cb.status] ?? "outline"}>{cb.status}</Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
