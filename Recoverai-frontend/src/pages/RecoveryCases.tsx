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
import { useRecoveryCases } from "@/hooks/useRecoveryOps";
import { useCampaigns } from "@/hooks/useCampaigns";
import { formatCurrency } from "@/lib/utils";

const STATUS_TONE: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  open: "default",
  closed: "secondary",
};

export default function RecoveryCases() {
  const [status, setStatus] = useState<string | undefined>("open");
  const [campaignId, setCampaignId] = useState<string | undefined>(undefined);
  const navigate = useNavigate();

  const { data, isLoading, isError } = useRecoveryCases(status, campaignId);
  const { data: campaignsData } = useCampaigns();

  return (
    <div className="space-y-6">
      <PageHeader title="Recovery Cases" description={data ? `${data.count} matching` : undefined} />

      <div className="flex gap-3">
        <Select value={status ?? "all"} onValueChange={(v) => setStatus(v === "all" ? undefined : v)}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="open">Open</SelectItem>
            <SelectItem value="closed">Closed</SelectItem>
          </SelectContent>
        </Select>

        <Select
          value={campaignId ?? "all"}
          onValueChange={(v) => setCampaignId(v === "all" ? undefined : v)}
        >
          <SelectTrigger className="w-56">
            <SelectValue placeholder="Campaign" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All campaigns</SelectItem>
            {campaignsData?.campaigns.map((c) => (
              <SelectItem key={c.id} value={String(c.id)}>
                {c.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load recovery cases. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/admin/recovery/cases/</code>.
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
                <TableHead>Status</TableHead>
                <TableHead>Priority</TableHead>
                <TableHead>Outcome</TableHead>
                <TableHead>Amount due</TableHead>
                <TableHead>Recovered</TableHead>
                <TableHead>Due date</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.cases.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                    No cases match these filters.
                  </TableCell>
                </TableRow>
              )}
              {data?.cases.map((c) => (
                <TableRow
                  key={c.id}
                  className="cursor-pointer"
                  onClick={() => navigate(`/customers/${c.customer_id}`)}
                >
                  <TableCell className="font-mono text-sm">#{c.customer_id}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_TONE[c.status] ?? "outline"}>{c.status}</Badge>
                  </TableCell>
                  <TableCell>{c.priority}</TableCell>
                  <TableCell className="text-muted-foreground">{c.outcome ?? "—"}</TableCell>
                  <TableCell className="font-mono text-sm">{formatCurrency(c.amount_due)}</TableCell>
                  <TableCell className="font-mono text-sm">{formatCurrency(c.amount_recovered)}</TableCell>
                  <TableCell className="text-muted-foreground text-sm">{c.due_date ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
