import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft, Coins } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCallDetail } from "@/hooks/useRecordings";
import { formatCurrency } from "@/lib/utils";

export default function CallDetail() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const { data, isLoading, isError } = useCallDetail(sessionId);

  return (
    <div className="space-y-6">
      <Button variant="ghost" size="sm" className="gap-1 -ml-2" onClick={() => navigate("/recordings")}>
        <ArrowLeft className="h-4 w-4" />
        Back to recordings
      </Button>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load this call. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/admin/calls/{sessionId}/</code>.
        </p>
      )}

      {data && (
        <>
          <PageHeader
            title={data.call.customer?.name ?? "Unknown customer"}
            description={data.call.customer?.phone_number ?? sessionId}
            actions={
              <div className="flex gap-2">
                <Badge variant="outline">{data.call.status}</Badge>
                {data.call.recovery_outcome && (
                  <Badge variant="secondary">{data.call.recovery_outcome}</Badge>
                )}
              </div>
            }
          />

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader>
                <CardTitle className="text-base">Recording</CardTitle>
              </CardHeader>
              <CardContent>
                {data.call.recording_mixed ? (
                  // Relative path -> goes through Vite's dev proxy -> same-origin
                  // from the browser's POV -> session cookie rides along.
                  // Hitting http://localhost:8000 directly here would be a
                  // cross-origin request and silently drop the cookie, which
                  // is exactly the "audio won't load, no console error" bug.
                  <audio
                    controls
                    className="w-full"
                    src={`/api/admin/recordings/${sessionId}/audio/`}
                  >
                    Your browser doesn't support audio playback.
                  </audio>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    No recording file for this call.
                  </p>
                )}
                {data.call.call_summary && (
                  <p className="text-sm mt-4 text-muted-foreground">{data.call.call_summary}</p>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base flex items-center gap-2">
                  <Coins className="size-4 text-muted-foreground" />
                  Cost breakdown
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <CostRow label="STT" value={data.call.stt_pricing} />
                <CostRow label="TTS" value={data.call.tts_pricing} />
                <CostRow label="LLM" value={data.call.llm_pricing} />
                <CostRow label="Dialer" value={data.call.dialer_pricing} />
                <div className="pt-2 border-t">
                  <CostRow label="Total" value={data.call.total_cost} emphasize />
                </div>
                <div className="pt-2 border-t text-muted-foreground">
                  <div className="flex justify-between">
                    <span>Duration</span>
                    <span className="font-mono">
                      {data.call.duration_seconds ? `${data.call.duration_seconds}s` : "—"}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>Intent</span>
                    <span>{data.call.intent || "—"}</span>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Transcript</CardTitle>
            </CardHeader>
            <CardContent>
              {data.call.turns.length === 0 ? (
                <p className="text-sm text-muted-foreground">No turns recorded for this call.</p>
              ) : (
                <div className="space-y-3">
                  {data.call.turns.map((t) => {
                    const isCustomer = t.speaker?.toLowerCase() === "customer";
                    return (
                      <div
                        key={t.id}
                        className={`flex ${isCustomer ? "justify-end" : "justify-start"}`}
                      >
                        <div
                          className={`max-w-[75%] rounded-lg px-3 py-2 text-sm ${
                            isCustomer ? "bg-primary/10" : "bg-muted"
                          }`}
                        >
                          <div className="flex items-center gap-2 text-xs text-muted-foreground mb-1">
                            <span className="font-medium">{t.speaker}</span>
                            {t.intent && <Badge variant="outline" className="text-[10px] px-1 py-0">{t.intent}</Badge>}
                            {t.at && <span>{new Date(t.at).toLocaleTimeString()}</span>}
                          </div>
                          <div>{t.text}</div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

function CostRow({ label, value, emphasize }: { label: string; value: string; emphasize?: boolean }) {
  return (
    <div className="flex justify-between">
      <span className={emphasize ? "font-medium" : "text-muted-foreground"}>{label}</span>
      <span className={emphasize ? "font-semibold tabular-nums" : "font-mono tabular-nums"}>
        {formatCurrency(value)}
      </span>
    </div>
  );
}
