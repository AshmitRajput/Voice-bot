import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCreateCampaign } from "@/hooks/useCampaigns";

export default function NewCampaign() {
  const navigate = useNavigate();
  const createCampaign = useCreateCampaign();

  const [name, setName] = useState("");
  const [campaignType, setCampaignType] = useState("payment");
  const [description, setDescription] = useState("");
  const [dueWithinDays, setDueWithinDays] = useState(14);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    createCampaign.mutate(
      {
        name,
        campaign_type: campaignType,
        description,
        target_due_within_days: dueWithinDays,
      },
      {
        onSuccess: (res) => navigate(`/campaigns/${res.campaign.id}`),
      }
    );
  };

  return (
    <div className="space-y-6 max-w-lg">
      <Button variant="ghost" size="sm" className="gap-1 -ml-2" onClick={() => navigate("/campaigns")}>
        <ArrowLeft className="h-4 w-4" />
        Back to campaigns
      </Button>

      <PageHeader title="New campaign" />

      <Card>
        <CardContent className="pt-6">
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="March overdue sweep"
                required
              />
            </div>

            <div className="space-y-1.5">
              <Label>Campaign type</Label>
              <Select value={campaignType} onValueChange={setCampaignType}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="payment">Payment</SelectItem>
                  <SelectItem value="reminder">Reminder</SelectItem>
                  <SelectItem value="settlement">Settlement</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="description">Description</Label>
              <Input
                id="description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Optional"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="due-days">Target due-within (days)</Label>
              <Input
                id="due-days"
                type="number"
                min={1}
                value={dueWithinDays}
                onChange={(e) => setDueWithinDays(Number(e.target.value))}
              />
            </div>

            {createCampaign.isError && (
              <p className="text-sm text-destructive">
                Couldn't create the campaign — check the name isn't empty and try again.
              </p>
            )}

            <Button type="submit" disabled={createCampaign.isPending || !name}>
              {createCampaign.isPending ? "Creating…" : "Create campaign"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
