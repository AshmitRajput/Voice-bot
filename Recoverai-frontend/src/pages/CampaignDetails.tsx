import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCampaignDetail, useUpdateCampaign } from "@/hooks/useCampaigns";
import { formatCurrency } from "@/lib/utils";

const STATUSES = ["draft", "active", "paused", "finished"];

export default function CampaignDetails() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data, isLoading, isError } = useCampaignDetail(id);
  const updateCampaign = useUpdateCampaign(id);

  return (
    <div className="space-y-6">
      <Button variant="ghost" size="sm" className="gap-1 -ml-2" onClick={() => navigate("/campaigns")}>
        <ArrowLeft className="h-4 w-4" />
        Back to campaigns
      </Button>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load this campaign. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/admin/campaigns/{id}/</code>.
        </p>
      )}

      {data && (
        <>
          <div className="flex items-start justify-between">
            <PageHeader title={data.campaign.name} description={data.campaign.description || undefined} />
            <div className="flex items-center gap-2">
              <Badge variant="outline">{data.campaign.campaign_type}</Badge>
              <Select
                value={data.campaign.status}
                onValueChange={(v) => updateCampaign.mutate({ status: v })}
              >
                <SelectTrigger className="w-36">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {STATUSES.map((s) => (
                    <SelectItem key={s} value={s}>
                      {s}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Customers</CardTitle>
              </CardHeader>
              <CardContent className="text-2xl font-semibold tabular-nums">
                {data.campaign.customer_count}
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Calls attempted</CardTitle>
              </CardHeader>
              <CardContent className="text-2xl font-semibold tabular-nums">
                {data.campaign.calls_attempted}
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Calls connected</CardTitle>
              </CardHeader>
              <CardContent className="text-2xl font-semibold tabular-nums">
                {data.campaign.calls_connected}
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">Recovered</CardTitle>
              </CardHeader>
              <CardContent className="text-2xl font-semibold tabular-nums">
                {formatCurrency(data.campaign.amount_recovered)}
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Timeline</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-2 text-sm md:grid-cols-3">
              <div>
                <div className="text-muted-foreground">Created</div>
                <div>{data.campaign.created_at ?? "—"}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Started</div>
                <div>{data.campaign.started_at ?? "Not started"}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Finished</div>
                <div>{data.campaign.finished_at ?? "In progress"}</div>
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
