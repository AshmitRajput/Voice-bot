import { useCallback, useEffect, useRef, useState } from "react";
import { PageHeader } from "@/components/layout/AppShell";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { usePersonas } from "@/hooks/usePersonas";

/**
 * Voice Test — demo-only persona preview.
 *
 * Deliberately NOT wired to any customer/phone number: connects with
 * ?persona_id=<id>&demo=1, which the patched consumers.py routes down a
 * path that never touches a real Customer/RecoveryCase row and never
 * creates a CallSession — see the backend patch notes. This page exists
 * to sanity-check a persona's opening line, tone, and escalation
 * behaviour, not to simulate a real recovery call against real data.
 *
 * WS protocol (unchanged from the original VoiceTestPage.jsx):
 *   client -> server : raw 16kHz mono PCM16 binary frames (mic audio)
 *                       JSON: {type:"init"|"playback_start"|"playback_end"|"interrupt"}
 *   server -> client : JSON: transcript / pcm_start / pcm_end / ai_response
 *                       / bot_interrupted / no_speech / error / done / call_ended
 *                       binary: raw 24kHz mono PCM16 bot audio frames
 *
 * Requires public/pcm-processor.js (unchanged, already in your project).
 */

// Everything runs locally right now (Django dev server on :8000), so this
// is hardcoded rather than read from an env var. If you later deploy the
// backend anywhere else, change this one line.
const WS_BASE = "ws://localhost:8000";
const BOT_SAMPLE_RATE = 24000;
const MIC_SAMPLE_RATE = 16000;

export default function VoiceTest() {
  const { data: personas } = usePersonas();
  const [personaId, setPersonaId] = useState<string>("");
  const [status, setStatus] = useState<
    "idle" | "connecting" | "listening" | "speaking" | "processing" | "error" | "ended"
  >("idle");
  const [transcriptLog, setTranscriptLog] = useState<
    { speaker: string; text: string; at: number }[]
  >([]);
  const [errorMsg, setErrorMsg] = useState("");

  const wsRef = useRef<WebSocket | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const micContextRef = useRef<AudioContext | null>(null);
  const micProcessorRef = useRef<{
    stream: MediaStream;
    processor: ScriptProcessorNode;
    source: MediaStreamAudioSourceNode;
  } | null>(null);
  const workletCtxRef = useRef<AudioContext | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);

  const appendLog = useCallback((speaker: string, text: string) => {
    setTranscriptLog((prev) => [...prev, { speaker, text, at: Date.now() }]);
  }, []);

  const initWorklet = useCallback(async () => {
    if (workletNodeRef.current && workletCtxRef.current) return true;
    try {
      const ctx = new AudioContext({ sampleRate: BOT_SAMPLE_RATE });
      await ctx.audioWorklet.addModule("/pcm-processor.js");
      const node = new AudioWorkletNode(ctx, "pcm-processor");
      node.connect(ctx.destination);
      node.port.onmessage = (e) => {
        if (e.data?.type === "ended") {
          setStatus("listening");
          sendControl("playback_end");
        }
      };
      workletCtxRef.current = ctx;
      workletNodeRef.current = node;
      return true;
    } catch (err: any) {
      setErrorMsg(`AudioWorklet init failed: ${err.message}`);
      return false;
    }
  }, []);

  const sendControl = useCallback((type: string) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type }));
    }
  }, []);

  const downsample = (float32: Float32Array, fromRate: number, toRate: number) => {
    if (fromRate === toRate) return float32;
    const ratio = fromRate / toRate;
    const newLen = Math.floor(float32.length / ratio);
    const out = new Float32Array(newLen);
    for (let i = 0; i < newLen; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(Math.floor((i + 1) * ratio), float32.length);
      let sum = 0;
      for (let j = start; j < end; j++) sum += float32[j];
      out[i] = sum / (end - start || 1);
    }
    return out;
  };

  const floatToInt16 = (float32: Float32Array) => {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  };

  const startMic = useCallback(async () => {
    if (micProcessorRef.current) return true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      micStreamRef.current = stream;

      const ctx = new AudioContext();
      micContextRef.current = ctx;
      const actualRate = ctx.sampleRate;
      const source = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);

      processor.onaudioprocess = (e) => {
        const input = e.inputBuffer.getChannelData(0);
        const down = downsample(input, actualRate, MIC_SAMPLE_RATE);
        const int16 = floatToInt16(down);
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(int16.buffer);
        }
      };

      source.connect(processor);
      processor.connect(ctx.destination);
      micProcessorRef.current = { stream, processor, source };
      return true;
    } catch (err: any) {
      setErrorMsg(`Microphone access failed: ${err.message}`);
      return false;
    }
  }, []);

  const stopMic = useCallback(() => {
    if (micProcessorRef.current) {
      const { stream, processor, source } = micProcessorRef.current;
      source?.disconnect();
      processor?.disconnect();
      stream?.getTracks().forEach((t) => t.stop());
      micProcessorRef.current = null;
    }
    if (micContextRef.current) {
      micContextRef.current.close().catch(() => {});
      micContextRef.current = null;
    }
  }, []);

  const handleMessage = useCallback(
    async (event: MessageEvent) => {
      if (event.data instanceof Blob) {
        const buf = await event.data.arrayBuffer();
        workletNodeRef.current?.port.postMessage(buf);
        return;
      }
      if (event.data instanceof ArrayBuffer) {
        workletNodeRef.current?.port.postMessage(event.data);
        return;
      }

      let data: any;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }

      switch (data.type) {
        case "transcript":
          appendLog("customer", data.text);
          setStatus("processing");
          break;
        case "pcm_start":
          setStatus("speaking");
          await initWorklet();
          break;
        case "pcm_end":
          workletNodeRef.current?.port.postMessage({ type: "end" });
          break;
        case "ai_response":
          appendLog("agent", data.text);
          break;
        case "bot_interrupted":
          workletNodeRef.current?.port.postMessage({ type: "clear" });
          setStatus("listening");
          break;
        case "no_speech":
          setStatus("listening");
          break;
        case "error":
          setErrorMsg(data.message || "Unknown server error");
          setStatus("error");
          break;
        case "call_ended":
          setStatus("ended");
          break;
        default:
          break;
      }
    },
    [appendLog, initWorklet]
  );

  const connect = useCallback(async () => {
    if (!personaId) {
      setErrorMsg("Pick a persona first.");
      return;
    }
    setErrorMsg("");
    setTranscriptLog([]);
    setStatus("connecting");

    // demo=1 is what routes consumers.py's connect() away from any real
    // customer/CallSession resolution — see backend patch notes.
    const url = `${WS_BASE}/api/voice/ws/audio?persona_id=${encodeURIComponent(personaId)}&demo=1`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = async () => {
      setStatus("listening");
      await initWorklet();
      await startMic();
    };
    ws.onmessage = handleMessage;
    ws.onerror = () => {
      setErrorMsg("WebSocket error — check backend is running on :8000");
      setStatus("error");
    };
    ws.onclose = () => {
      setStatus((s) => (s === "ended" ? s : "idle"));
      stopMic();
    };
  }, [personaId, handleMessage, initWorklet, startMic, stopMic]);

  const disconnect = useCallback(() => {
    wsRef.current?.close();
    wsRef.current = null;
    stopMic();
    if (workletCtxRef.current) {
      workletCtxRef.current.close().catch(() => {});
      workletCtxRef.current = null;
      workletNodeRef.current = null;
    }
    setStatus("idle");
  }, [stopMic]);

  useEffect(() => () => disconnect(), []); // eslint-disable-line react-hooks/exhaustive-deps

  const isLive = status !== "idle" && status !== "ended";

  return (
    <div className="space-y-6 max-w-2xl">
      <PageHeader
        title="AI Voice Test"
        description="Preview a persona's conversation flow — opening line, tone, and escalation. Not linked to any customer or case."
      />

      <Card>
        <CardContent className="pt-6 space-y-4">
          <div className="flex items-end gap-3">
            <div className="flex-1 space-y-1.5">
              <label className="text-sm font-medium">Persona</label>
              <Select value={personaId} onValueChange={setPersonaId} disabled={isLive}>
                <SelectTrigger>
                  <SelectValue placeholder="Choose a persona to test" />
                </SelectTrigger>
                <SelectContent>
                  {personas?.settings.map((p) => (
                    <SelectItem key={p.id} value={String(p.id)}>
                      {p.persona_name || p.name}
                      {p.is_active ? " (active)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {!isLive ? (
              <Button onClick={connect} disabled={!personaId}>
                Start demo call
              </Button>
            ) : (
              <Button variant="destructive" onClick={disconnect}>
                End call
              </Button>
            )}
          </div>

          <div className="flex items-center gap-2 text-sm">
            <span
              className={`inline-block h-2.5 w-2.5 rounded-full ${
                {
                  idle: "bg-muted-foreground",
                  connecting: "bg-amber-500",
                  listening: "bg-emerald-500",
                  speaking: "bg-blue-500",
                  processing: "bg-violet-500",
                  error: "bg-destructive",
                  ended: "bg-muted-foreground",
                }[status]
              }`}
            />
            <span className="font-medium">{status}</span>
          </div>

          {errorMsg && <p className="text-sm text-destructive">{errorMsg}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-6">
          <h3 className="text-sm font-medium mb-3">Transcript</h3>
          {transcriptLog.length === 0 && (
            <p className="text-sm text-muted-foreground">No conversation yet.</p>
          )}
          <div className="space-y-2">
            {transcriptLog.map((entry, i) => (
              <div key={i} className={entry.speaker === "customer" ? "text-right" : "text-left"}>
                <span
                  className={`inline-block max-w-[80%] rounded-xl px-3 py-2 text-sm ${
                    entry.speaker === "customer" ? "bg-primary/10" : "bg-muted"
                  }`}
                >
                  <span className="block text-[11px] opacity-60 font-medium">
                    {entry.speaker === "customer" ? "You" : "Agent"}
                  </span>
                  {entry.text}
                </span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">
        Connects to {WS_BASE}/api/voice/ws/audio?demo=1 — this call is not
        linked to any customer record and never creates a call log or
        recording.
      </p>
    </div>
  );
}