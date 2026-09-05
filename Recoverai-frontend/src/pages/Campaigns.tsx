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
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCampaigns } from "@/hooks/useCampaigns";
import { formatCurrency } from "@/lib/utils";

const STATUS_TONE: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  draft: "outline",
  active: "default",
  paused: "secondary",
  finished: "secondary",
};

export default function Campaigns() {
  const [status, setStatus] = useState<string | undefined>(undefined);
  const { data, isLoading, isError } = useCampaigns(status);
  const navigate = useNavigate();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <PageHeader
          title="Campaigns"
          description={data ? `${data.count} total` : undefined}
        />
        <Button onClick={() => navigate("/campaigns/new")}>New campaign</Button>
      </div>

      <Select
        value={status ?? "all"}
        onValueChange={(v) => setStatus(v === "all" ? undefined : v)}
      >
        <SelectTrigger className="w-48">
          <SelectValue placeholder="Filter by status" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All statuses</SelectItem>
          <SelectItem value="draft">Draft</SelectItem>
          <SelectItem value="active">Active</SelectItem>
          <SelectItem value="paused">Paused</SelectItem>
          <SelectItem value="finished">Finished</SelectItem>
        </SelectContent>
      </Select>

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load campaigns. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/admin/campaigns/</code>.
        </p>
      )}

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Customers</TableHead>
                <TableHead>Calls attempted</TableHead>
                <TableHead>Recovered</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.campaigns.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                    No campaigns yet — create one to get started.
                  </TableCell>
                </TableRow>
              )}
              {data?.campaigns.map((c) => (
                <TableRow
                  key={c.id}
                  className="cursor-pointer"
                  onClick={() => navigate(`/campaigns/${c.id}`)}
                >
                  <TableCell className="font-medium">{c.name}</TableCell>
                  <TableCell className="text-muted-foreground">{c.campaign_type}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_TONE[c.status] ?? "default"}>{c.status}</Badge>
                  </TableCell>
                  <TableCell className="tabular-nums">{c.customer_count}</TableCell>
                  <TableCell className="tabular-nums">{c.calls_attempted}</TableCell>
                  <TableCell className="font-mono text-sm">
                    {formatCurrency(c.amount_recovered)}
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
