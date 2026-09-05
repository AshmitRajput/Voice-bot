import { useState } from "react";
import { Plus, Play, Pencil, Trash2, Loader2 } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { useVoices, useCreateVoice, useUpdateVoice, useDeleteVoice, useTestTts } from "@/hooks/useVoices";
import type { TTSVoice, TTSVoiceWritePayload } from "@/lib/types";

const GENDERS = ["male", "female"];
const LANGUAGES = ["hi-IN", "en-IN"];

const emptyForm: TTSVoiceWritePayload = {
  voice_name: "",
  gender: "female",
  provider_voice_id: "",
  provider_name: "Murf",
  language: "hi-IN",
  is_active: true,
  sample_url: "",
};

export default function Voices() {
  const { data, isLoading, isError } = useVoices();
  const createVoice = useCreateVoice();
  const updateVoice = useUpdateVoice();
  const deleteVoice = useDeleteVoice();
  const testTts = useTestTts();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<TTSVoice | null>(null);
  const [form, setForm] = useState<TTSVoiceWritePayload>(emptyForm);
  const [previewingId, setPreviewingId] = useState<number | null>(null);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm);
    setDialogOpen(true);
  };

  const openEdit = (v: TTSVoice) => {
    setEditing(v);
    setForm({
      voice_name: v.voice_name,
      gender: v.gender,
      provider_voice_id: v.provider_voice_id,
      provider_name: v.provider_name,
      language: v.language,
      is_active: v.is_active,
      sample_url: v.sample_url,
    });
    setDialogOpen(true);
  };

  const submit = async () => {
    if (editing) {
      await updateVoice.mutateAsync({ id: editing.id, payload: form });
    } else {
      await createVoice.mutateAsync(form);
    }
    setDialogOpen(false);
  };

  const preview = async (v: TTSVoice) => {
    setPreviewingId(v.id);
    try {
      const res = await testTts.mutateAsync({
        provider: v.provider_name.toLowerCase(),
        voice: v.provider_voice_id || v.voice_name,
      });
      const audio = new Audio(`data:audio/mpeg;base64,${res.audio_b64}`);
      await audio.play();
    } finally {
      setPreviewingId(null);
    }
  };

  const saving = createVoice.isPending || updateVoice.isPending;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Voices"
        description={data ? `${data.count} configured` : undefined}
        actions={
          <Button onClick={openCreate}>
            <Plus className="size-4" /> New voice
          </Button>
        }
      />

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load voices. Check <code className="rounded bg-muted px-1 py-0.5">/api/admin/tts-voices/</code>.
        </p>
      )}

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Voice</TableHead>
                <TableHead>Gender</TableHead>
                <TableHead>Provider</TableHead>
                <TableHead>Language</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.voices.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground py-8">
                    No voices yet — add one to assign it to a persona.
                  </TableCell>
                </TableRow>
              )}
              {data?.voices.map((v) => (
                <TableRow key={v.id}>
                  <TableCell className="font-medium">{v.voice_name}</TableCell>
                  <TableCell className="capitalize text-muted-foreground">{v.gender}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {v.provider_name}
                    {v.provider_voice_id && (
                      <div className="text-xs font-mono">{v.provider_voice_id}</div>
                    )}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{v.language}</TableCell>
                  <TableCell>
                    {v.is_active ? (
                      <Badge className="bg-[color:var(--success)]/12 text-[color:var(--success)]">Active</Badge>
                    ) : (
                      <Badge variant="outline">Inactive</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => preview(v)}
                        disabled={previewingId === v.id}
                        aria-label="Preview voice"
                      >
                        {previewingId === v.id ? (
                          <Loader2 className="size-4 animate-spin" />
                        ) : (
                          <Play className="size-4" />
                        )}
                      </Button>
                      <Button variant="ghost" size="icon" onClick={() => openEdit(v)} aria-label="Edit voice">
                        <Pencil className="size-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => deleteVoice.mutate(v.id)}
                        aria-label="Delete voice"
                      >
                        <Trash2 className="size-4 text-destructive" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit voice" : "New voice"}</DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label>Voice name</Label>
              <Input
                value={form.voice_name}
                onChange={(e) => setForm((f) => ({ ...f, voice_name: e.target.value }))}
                placeholder="e.g. Priya — Warm Hindi"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Gender</Label>
                <Select value={form.gender} onValueChange={(v) => setForm((f) => ({ ...f, gender: v }))}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {GENDERS.map((g) => (
                      <SelectItem key={g} value={g} className="capitalize">{g}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Language</Label>
                <Select value={form.language} onValueChange={(v) => setForm((f) => ({ ...f, language: v }))}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {LANGUAGES.map((l) => (
                      <SelectItem key={l} value={l}>{l}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Provider</Label>
                <Input
                  value={form.provider_name}
                  onChange={(e) => setForm((f) => ({ ...f, provider_name: e.target.value }))}
                  placeholder="Murf"
                />
              </div>
              <div className="space-y-1.5">
                <Label>Provider voice ID</Label>
                <Input
                  value={form.provider_voice_id}
                  onChange={(e) => setForm((f) => ({ ...f, provider_voice_id: e.target.value }))}
                  placeholder="hi-IN-Wavenet-A"
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>Sample URL (optional)</Label>
              <Input
                value={form.sample_url}
                onChange={(e) => setForm((f) => ({ ...f, sample_url: e.target.value }))}
              />
            </div>

            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <Label htmlFor="voice-active" className="cursor-pointer">Active</Label>
              <Switch
                id="voice-active"
                checked={!!form.is_active}
                onCheckedChange={(v) => setForm((f) => ({ ...f, is_active: v }))}
              />
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button>
            <Button onClick={submit} disabled={saving || !form.voice_name}>
              {saving && <Loader2 className="size-4 animate-spin" />}
              {editing ? "Save changes" : "Create voice"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
