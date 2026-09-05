import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Input } from "@/components/ui/input";
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
import { useRecordings } from "@/hooks/useRecordings";
import { useCampaigns } from "@/hooks/useCampaigns";

// CallSession.status choices — confirmed against models.py in an earlier
// phase. If these drift, the Select still degrades gracefully (an
// unmatched value just won't highlight any option).
const STATUS_OPTIONS = [
  "queued", "ringing", "ongoing", "completed", "failed", "busy", "no_answer", "dropped",
];

const STATUS_TONE: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  completed: "secondary",
  ongoing: "default",
  failed: "destructive",
  dropped: "destructive",
  no_answer: "outline",
  busy: "outline",
  queued: "outline",
  ringing: "default",
};

function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function CallRecordings() {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<string | undefined>(undefined);
  const [campaignId, setCampaignId] = useState<string | undefined>(undefined);
  const navigate = useNavigate();

  const { data, isLoading, isError, isFetching } = useRecordings(page, {
    search: search || undefined,
    status,
    campaignId,
  });
  const { data: campaignsData } = useCampaigns();

  const totalPages = data ? Math.max(1, Math.ceil(data.count / 25)) : 1;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Call Recordings"
        description={data ? `${data.count} calls` : undefined}
      />

      <div className="flex flex-wrap gap-3">
        <div className="relative max-w-sm flex-1 min-w-[220px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search customer name or phone…"
            className="pl-8"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
          />
        </div>

        <Select
          value={status ?? "all"}
          onValueChange={(v) => {
            setStatus(v === "all" ? undefined : v);
            setPage(1);
          }}
        >
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            {STATUS_OPTIONS.map((s) => (
              <SelectItem key={s} value={s}>
                {s.replace("_", " ")}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={campaignId ?? "all"}
          onValueChange={(v) => {
            setCampaignId(v === "all" ? undefined : v);
            setPage(1);
          }}
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
          Couldn't load recordings. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/admin/recordings/</code>{" "}
          is reachable.
        </p>
      )}

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Customer</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Intent</TableHead>
                  <TableHead>Outcome</TableHead>
                  <TableHead>Duration</TableHead>
                  <TableHead>Started</TableHead>
                  <TableHead>Recording</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data?.results.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                      No calls match these filters.
                    </TableCell>
                  </TableRow>
                )}
                {data?.results.map((r) => (
                  <TableRow
                    key={r.session_id}
                    className="cursor-pointer"
                    onClick={() => navigate(`/recordings/${r.session_id}`)}
                  >
                    <TableCell className="font-medium">
                      {r.customer?.name ?? "Unknown"}
                      <div className="text-xs text-muted-foreground font-mono">
                        {r.customer?.phone_number ?? "—"}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={STATUS_TONE[r.status] ?? "outline"}>{r.status}</Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground">{r.intent || "—"}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {r.recovery_outcome || "—"}
                    </TableCell>
                    <TableCell className="font-mono text-sm">
                      {formatDuration(r.duration_seconds)}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {r.started_at ? new Date(r.started_at).toLocaleString() : "—"}
                    </TableCell>
                    <TableCell>
                      {r.recording_mixed || r.recording_stereo ? (
                        <Badge variant="outline">Available</Badge>
                      ) : (
                        <span className="text-muted-foreground text-sm">None</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">
              Page {page} of {totalPages}
              {isFetching && " · refreshing…"}
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}