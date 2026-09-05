import { useEffect, useState } from "react";
import { Plus, Loader2, Sparkles } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { usePersonas, useCreatePersona, useUpdatePersona } from "@/hooks/usePersonas";
import { useVoices } from "@/hooks/useVoices";
import type { LLMSetting, LLMSettingWritePayload } from "@/lib/types";
import { cn } from "@/lib/utils";

const PROVIDERS = ["gemini", "openai", "krutrim", "bharatrouter"];
const LANGUAGES = ["hi-IN", "en-IN"];

function toForm(s: LLMSetting): LLMSettingWritePayload {
  return {
    name: s.name,
    is_active: s.is_active,
    provider: s.provider,
    model: s.model,
    temperature: s.temperature,
    max_tokens: s.max_tokens,
    persona_name: s.persona_name,
    opening_line: s.opening_line,
    system_prompt: s.system_prompt,
    behaviour: s.behaviour,
    tone: s.tone,
    pace: s.pace,
    barge_in_threshold: s.barge_in_threshold,
    max_turns: s.max_turns,
    allow_customer_barge_in: s.allow_customer_barge_in,
    language: s.language,
    response_max_chars: s.response_max_chars,
    questions_per_turn_max: s.questions_per_turn_max,
    voice_id: s.voice?.id,
  };
}

const emptyForm: LLMSettingWritePayload = {
  name: "default",
  is_active: true,
  provider: "gemini",
  model: "gemini-2.5-flash-lite",
  temperature: 0.4,
  max_tokens: 1000,
  persona_name: "",
  opening_line: "",
  system_prompt: "",
  behaviour: "",
  tone: 72,
  pace: 50,
  barge_in_threshold: 65,
  max_turns: 10,
  allow_customer_barge_in: true,
  language: "hi-IN",
  response_max_chars: 240,
  questions_per_turn_max: 1,
};

export default function Personas() {
  const { data, isLoading, isError } = usePersonas();
  const { data: voicesData } = useVoices();
  const createPersona = useCreatePersona();
  const updatePersona = useUpdatePersona();

  const [selectedId, setSelectedId] = useState<number | "new" | null>(null);
  const [form, setForm] = useState<LLMSettingWritePayload>(emptyForm);

  const personas = data?.settings ?? [];
  const selected = personas.find((p) => p.id === selectedId) ?? null;

  // Auto-select the active persona (or the first one) once the list loads.
  useEffect(() => {
    if (selectedId !== null || personas.length === 0) return;
    const active = personas.find((p) => p.is_active) ?? personas[0];
    setSelectedId(active.id);
  }, [personas, selectedId]);

  useEffect(() => {
    if (selectedId === "new") setForm(emptyForm);
    else if (selected) setForm(toForm(selected));
  }, [selectedId, selected]);

  const saving = createPersona.isPending || updatePersona.isPending;

  const submit = async () => {
    if (selectedId === "new") {
      const res = await createPersona.mutateAsync(form);
      setSelectedId(res.setting.id);
    } else if (selected) {
      await updatePersona.mutateAsync({ id: selected.id, payload: form });
    }
  };

  return (
    <div className="grid lg:grid-cols-[280px_1fr] min-h-[calc(100vh-3.5rem)]">
      {/* Persona list */}
      <div className="border-r p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Personas</h2>
          <Button size="sm" variant="outline" onClick={() => setSelectedId("new")}>
            <Plus className="size-3.5" /> New
          </Button>
        </div>

        {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
        {isError && <p className="text-sm text-destructive">Couldn't load personas.</p>}

        <div className="space-y-1">
          {personas.map((p) => (
            <button
              key={p.id}
              onClick={() => setSelectedId(p.id)}
              className={cn(
                "w-full text-left rounded-md px-3 py-2 text-sm transition-colors",
                selectedId === p.id ? "bg-accent" : "hover:bg-accent/50",
              )}
            >
              <div className="flex items-center gap-2">
                <span className="font-medium truncate">{p.persona_name}</span>
                {p.is_active && (
                  <Badge className="ml-auto bg-[color:var(--success)]/12 text-[color:var(--success)] text-[10px]">
                    Active
                  </Badge>
                )}
              </div>
              <div className="text-xs text-muted-foreground truncate">{p.voice?.voice_name ?? "No voice set"}</div>
            </button>
          ))}
        </div>
      </div>

      {/* Editor */}
      <div className="p-4 md:p-6 lg:p-8 space-y-6">
        <PageHeader
          title={selectedId === "new" ? "New persona" : selected?.persona_name ?? "Select a persona"}
          description="Agent system prompt, voice, and conversation behaviour."
          actions={
            (selectedId === "new" || selected) && (
              <Button onClick={submit} disabled={saving || !form.persona_name || !form.system_prompt}>
                {saving && <Loader2 className="size-4 animate-spin" />}
                {selectedId === "new" ? "Create persona" : "Save changes"}
              </Button>
            )
          }
        />

        {(selectedId === "new" || selected) && (
          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardContent className="pt-6 space-y-4">
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label>Persona name</Label>
                    <Input
                      value={form.persona_name}
                      onChange={(e) => setForm((f) => ({ ...f, persona_name: e.target.value }))}
                      placeholder="e.g. Priya — Friendly Recovery Agent"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label>Internal name</Label>
                    <Input
                      value={form.name}
                      onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                    />
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label>Opening line</Label>
                  <Textarea
                    rows={2}
                    value={form.opening_line}
                    onChange={(e) => setForm((f) => ({ ...f, opening_line: e.target.value }))}
                    placeholder="Namaste, main RecoverAI se baat kar rahi hoon…"
                  />
                </div>

                <div className="space-y-1.5">
                  <Label>System prompt</Label>
                  <Textarea
                    rows={8}
                    value={form.system_prompt}
                    onChange={(e) => setForm((f) => ({ ...f, system_prompt: e.target.value }))}
                    placeholder="You are RecoverAI, an outbound revenue-recovery voice agent…"
                    className="font-mono text-xs"
                  />
                </div>

                <div className="space-y-1.5">
                  <Label>Behaviour notes</Label>
                  <Textarea
                    rows={3}
                    value={form.behaviour}
                    onChange={(e) => setForm((f) => ({ ...f, behaviour: e.target.value }))}
                    placeholder="Never threaten. Ask at most one question at a time…"
                  />
                </div>
              </CardContent>
            </Card>

            <div className="space-y-4">
              <Card>
                <CardContent className="pt-6 space-y-4">
                  <div className="space-y-1.5">
                    <Label>Voice</Label>
                    <Select
                      value={form.voice_id ? String(form.voice_id) : undefined}
                      onValueChange={(v) => setForm((f) => ({ ...f, voice_id: Number(v) }))}
                    >
                      <SelectTrigger><SelectValue placeholder="Pick a voice…" /></SelectTrigger>
                      <SelectContent>
                        {voicesData?.voices.map((v) => (
                          <SelectItem key={v.id} value={String(v.id)}>
                            {v.voice_name} ({v.language})
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1.5">
                      <Label>Provider</Label>
                      <Select value={form.provider} onValueChange={(v) => setForm((f) => ({ ...f, provider: v }))}>
                        <SelectTrigger><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {PROVIDERS.map((p) => (
                            <SelectItem key={p} value={p}>{p}</SelectItem>
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

                  <div className="space-y-1.5">
                    <Label>Model</Label>
                    <Input
                      value={form.model}
                      onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))}
                    />
                  </div>

                  <div className="flex items-center justify-between rounded-md border px-3 py-2">
                    <Label className="cursor-pointer">Active persona</Label>
                    <Switch
                      checked={!!form.is_active}
                      onCheckedChange={(v) => setForm((f) => ({ ...f, is_active: v }))}
                    />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardContent className="pt-6 space-y-4">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <Sparkles className="size-4 text-[color:var(--ai)]" />
                    Conversation tuning
                  </div>

                  <RangeField
                    label="Tone (formal → warm)"
                    value={form.tone ?? 72}
                    onChange={(v) => setForm((f) => ({ ...f, tone: v }))}
                  />
                  <RangeField
                    label="Pace (slow → fast)"
                    value={form.pace ?? 50}
                    onChange={(v) => setForm((f) => ({ ...f, pace: v }))}
                  />
                  <RangeField
                    label="Barge-in threshold"
                    value={form.barge_in_threshold ?? 65}
                    onChange={(v) => setForm((f) => ({ ...f, barge_in_threshold: v }))}
                  />

                  <div className="flex items-center justify-between rounded-md border px-3 py-2">
                    <Label className="cursor-pointer">Allow customer barge-in</Label>
                    <Switch
                      checked={!!form.allow_customer_barge_in}
                      onCheckedChange={(v) => setForm((f) => ({ ...f, allow_customer_barge_in: v }))}
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1.5">
                      <Label>Max turns</Label>
                      <Input
                        type="number"
                        value={form.max_turns}
                        onChange={(e) => setForm((f) => ({ ...f, max_turns: Number(e.target.value) }))}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label>Max response chars</Label>
                      <Input
                        type="number"
                        value={form.response_max_chars}
                        onChange={(e) => setForm((f) => ({ ...f, response_max_chars: Number(e.target.value) }))}
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1.5">
                      <Label>Questions / turn (max)</Label>
                      <Input
                        type="number"
                        min={1}
                        value={form.questions_per_turn_max}
                        onChange={(e) => setForm((f) => ({ ...f, questions_per_turn_max: Number(e.target.value) }))}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label>Temperature</Label>
                      <Input
                        type="number"
                        step="0.1"
                        min={0}
                        max={2}
                        value={form.temperature}
                        onChange={(e) => setForm((f) => ({ ...f, temperature: Number(e.target.value) }))}
                      />
                    </div>
                  </div>
                </CardContent>
              </Card>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function RangeField({
  label, value, onChange,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-sm">
        <Label>{label}</Label>
        <span className="text-xs text-muted-foreground tabular-nums">{value}</span>
      </div>
      <input
        type="range"
        min={0}
        max={100}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-primary"
      />
    </div>
  );
}
