import { useEffect, useState } from "react";
import { LogOut, Loader2, Save, ShieldCheck } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { useAuth } from "@/hooks/useAuth";
import { useCallSettings, useUpdateCallSettings } from "@/hooks/useCallSettings";

export default function Settings() {
  const { user, logout } = useAuth();
  const { data, isLoading, isError } = useCallSettings();
  const updateSettings = useUpdateCallSettings();

  const [callTimeout, setCallTimeout] = useState("");
  const [maxDuration, setMaxDuration] = useState("");

  useEffect(() => {
    if (data) {
      setCallTimeout(String(data.settings.call_timeout));
      setMaxDuration(String(data.settings.max_call_duration));
    }
  }, [data]);

  const dirty =
    data &&
    (Number(callTimeout) !== data.settings.call_timeout ||
      Number(maxDuration) !== data.settings.max_call_duration);

  const save = () => {
    updateSettings.mutate({
      call_timeout: Number(callTimeout),
      max_call_duration: Number(maxDuration),
    });
  };

  return (
    <div className="space-y-6">
      <PageHeader title="Settings" description="Account and call behavior for this workspace." />

      <div className="p-4 md:p-6 lg:p-8 space-y-6 max-w-2xl">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Account</CardTitle>
            <CardDescription>Signed in as the workspace administrator.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-3">
              <Avatar className="size-10">
                <AvatarFallback className="bg-primary text-primary-foreground">
                  {(user?.name ?? "A").slice(0, 2).toUpperCase()}
                </AvatarFallback>
              </Avatar>
              <div>
                <div className="text-sm font-medium">{user?.name}</div>
                <div className="text-xs text-muted-foreground">{user?.email || user?.username}</div>
              </div>
              {user?.is_superuser && (
                <Badge className="ml-auto gap-1 bg-[color:var(--success)]/12 text-[color:var(--success)]">
                  <ShieldCheck className="size-3" /> Superuser
                </Badge>
              )}
            </div>

            <Button variant="outline" onClick={logout} className="text-destructive hover:text-destructive">
              <LogOut className="size-4" /> Sign out
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Call behavior</CardTitle>
            <CardDescription>
              Applies to every outbound call. Barge-in and voice tone live on each persona instead —
              see the Personas page.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {isError && (
              <p className="text-sm text-destructive">
                Couldn't load call settings. Check{" "}
                <code className="rounded bg-muted px-1 py-0.5">/api/admin/call-settings/</code>.
              </p>
            )}

            {isLoading ? (
              <p className="text-sm text-muted-foreground">Loading…</p>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1.5">
                    <Label>Ring timeout (seconds)</Label>
                    <Input
                      type="number"
                      min={5}
                      value={callTimeout}
                      onChange={(e) => setCallTimeout(e.target.value)}
                    />
                    <p className="text-[11px] text-muted-foreground">
                      How long to wait for the customer to pick up before marking the call no-answer.
                    </p>
                  </div>
                  <div className="space-y-1.5">
                    <Label>Max call duration (seconds)</Label>
                    <Input
                      type="number"
                      min={30}
                      value={maxDuration}
                      onChange={(e) => setMaxDuration(e.target.value)}
                    />
                    <p className="text-[11px] text-muted-foreground">
                      Hard cap on a single call's length, regardless of how the conversation is going.
                    </p>
                  </div>
                </div>

                <Button onClick={save} disabled={!dirty || updateSettings.isPending}>
                  {updateSettings.isPending ? (
                    <Loader2 className="size-4 animate-spin" />
                  ) : (
                    <Save className="size-4" />
                  )}
                  Save changes
                </Button>

                {updateSettings.isSuccess && !dirty && (
                  <p className="text-xs text-[color:var(--success)]">Saved.</p>
                )}
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
