import React, { useCallback, useEffect, useRef, useState } from "react";

/**
 * VoiceTestPage — RecoverAI internal test/demo page.
 *
 * Connects directly to the Django Channels WS consumer (VoiceChatConsumer,
 * consumers.py) and exercises the full STT -> LLM -> TTS pipeline from the
 * browser, same protocol as the old Oilvia frontend:
 *
 *   client -> server : raw 16kHz mono PCM16 binary frames (mic audio)
 *                       JSON control messages: {type:"init"|"playback_start"
 *                       |"playback_end"|"interrupt"}
 *   server -> client : JSON: transcript / pcm_start / pcm_end / ai_response
 *                       / bot_interrupted / no_speech / error / done /
 *                       call_ended
 *                       binary: raw 24kHz mono PCM16 bot audio frames
 *
 * Drop this file in as a route component. No extra dependencies (no axios,
 * no external audio libs) -- just fetch()/WebSocket and Web Audio API.
 *
 * Requires public/pcm-processor.js (AudioWorklet) -- included below as a
 * comment at the bottom of this file; save it separately.
 */

// ── Config ──────────────────────────────────────────────────────────
const WS_BASE = "ws://localhost:8000";
const HTTP_BASE = "http://localhost:8000";
const BOT_SAMPLE_RATE = 24000; // must match consumers.py BOT_AUDIO_SAMPLE_RATE
const MIC_SAMPLE_RATE = 16000; // must match consumers.py RECORD_SAMPLE_RATE

export default function VoiceTestPage() {
  const [status, setStatus] = useState("idle"); // idle | connecting | listening | speaking | processing | error | ended
  const [transcriptLog, setTranscriptLog] = useState([]); // {speaker, text}[]
  const [phoneNumber, setPhoneNumber] = useState("+919876543210");
  const [errorMsg, setErrorMsg] = useState("");
  const [healthInfo, setHealthInfo] = useState(null);

  const wsRef = useRef(null);
  const micStreamRef = useRef(null);
  const micContextRef = useRef(null);
  const micProcessorRef = useRef(null);
  const workletCtxRef = useRef(null);
  const workletNodeRef = useRef(null);
  const connectedRef = useRef(false);

  // ── Health check on mount (proves REST side is reachable) ──────────
  useEffect(() => {
    fetch(`${HTTP_BASE}/api/health/`)
      .then((r) => r.json())
      .then(setHealthInfo)
      .catch((e) => setHealthInfo({ status: "unreachable", error: String(e) }));
  }, []);

  const appendLog = useCallback((speaker, text) => {
    setTranscriptLog((prev) => [...prev, { speaker, text, at: Date.now() }]);
  }, []);

  // ── AudioWorklet init (bot playback) ────────────────────────────────
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
    } catch (err) {
      console.error("AudioWorklet init failed:", err);
      setErrorMsg(`AudioWorklet init failed: ${err.message}`);
      return false;
    }
  }, []);

  const sendControl = useCallback((type) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type }));
    }
  }, []);

  // ── Mic capture ──────────────────────────────────────────────────────
  const downsample = (float32, fromRate, toRate) => {
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

  const floatToInt16 = (float32) => {
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
    } catch (err) {
      console.error("Mic capture failed:", err);
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

  // ── WebSocket message handling ───────────────────────────────────────
  const handleMessage = useCallback(
    async (event) => {
      if (event.data instanceof Blob) {
        const buf = await event.data.arrayBuffer();
        workletNodeRef.current?.port.postMessage(buf);
        return;
      }
      if (event.data instanceof ArrayBuffer) {
        workletNodeRef.current?.port.postMessage(event.data);
        return;
      }

      let data;
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
          // server done sending; worklet's "ended" message (handled in
          // initWorklet) will flip status back to "listening" once actual
          // playback finishes.
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
        case "done":
          break;
        default:
          break;
      }
    },
    [appendLog, initWorklet]
  );

  // ── Connect / disconnect ─────────────────────────────────────────────
  const connect = useCallback(async () => {
    setErrorMsg("");
    setTranscriptLog([]);
    setStatus("connecting");

    const url = `${WS_BASE}/api/voice/ws/audio?phone=${encodeURIComponent(phoneNumber)}`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = async () => {
      connectedRef.current = true;
      setStatus("listening");
      await initWorklet();
      await startMic();
    };
    ws.onmessage = handleMessage;
    ws.onerror = (e) => {
      console.error("WS error:", e);
      setErrorMsg("WebSocket error — check backend is running on :8000");
      setStatus("error");
    };
    ws.onclose = (e) => {
      connectedRef.current = false;
      if (status !== "ended") setStatus("idle");
      stopMic();
    };
  }, [phoneNumber, handleMessage, initWorklet, startMic, status, stopMic]);

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

  const statusColor = {
    idle: "#888",
    connecting: "#e0a800",
    listening: "#28a745",
    speaking: "#007bff",
    processing: "#6f42c1",
    error: "#dc3545",
    ended: "#888",
  }[status];

  return (
    <div style={{ maxWidth: 720, margin: "40px auto", fontFamily: "system-ui, sans-serif" }}>
      <h2>RecoverAI — Voice Pipeline Test</h2>

      <div style={{ marginBottom: 16, padding: 12, background: "#f5f5f5", borderRadius: 8, fontSize: 13 }}>
        <strong>Backend health:</strong>{" "}
        {healthInfo ? (
          <span>
            {healthInfo.status} · redis: {healthInfo.redis} ·{" "}
            {healthInfo.timestamp}
          </span>
        ) : (
          "checking..."
        )}
      </div>

      <div style={{ marginBottom: 16 }}>
        <label style={{ marginRight: 8 }}>Test phone number:</label>
        <input
          value={phoneNumber}
          onChange={(e) => setPhoneNumber(e.target.value)}
          disabled={status !== "idle" && status !== "ended"}
          style={{ padding: "6px 10px", width: 200 }}
        />
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
        <span
          style={{
            display: "inline-block",
            width: 12,
            height: 12,
            borderRadius: "50%",
            background: statusColor,
          }}
        />
        <strong>{status}</strong>

        {status === "idle" || status === "ended" ? (
          <button onClick={connect} style={btnStyle}>
            Start Call
          </button>
        ) : (
          <button onClick={disconnect} style={{ ...btnStyle, background: "#dc3545" }}>
            End Call
          </button>
        )}
      </div>

      {errorMsg && (
        <div style={{ padding: 10, background: "#fdecea", color: "#611a15", borderRadius: 6, marginBottom: 16 }}>
          {errorMsg}
        </div>
      )}

      <div style={{ border: "1px solid #ddd", borderRadius: 8, padding: 16, minHeight: 240, background: "#fff" }}>
        <h4 style={{ marginTop: 0 }}>Transcript</h4>
        {transcriptLog.length === 0 && <p style={{ color: "#999" }}>No conversation yet.</p>}
        {transcriptLog.map((entry, i) => (
          <div
            key={i}
            style={{
              marginBottom: 8,
              textAlign: entry.speaker === "customer" ? "right" : "left",
            }}
          >
            <span
              style={{
                display: "inline-block",
                padding: "8px 12px",
                borderRadius: 12,
                background: entry.speaker === "customer" ? "#e7f0ff" : "#f0f0f0",
                maxWidth: "80%",
              }}
            >
              <strong style={{ fontSize: 11, display: "block", opacity: 0.6 }}>
                {entry.speaker === "customer" ? "You" : "Agent"}
              </strong>
              {entry.text}
            </span>
          </div>
        ))}
      </div>

      <p style={{ fontSize: 12, color: "#999", marginTop: 16 }}>
        Connects to {WS_BASE}/api/voice/ws/audio — make sure `python manage.py runserver`
        is running and Redis is up before starting a call.
      </p>
    </div>
  );
}

const btnStyle = {
  padding: "8px 18px",
  borderRadius: 6,
  border: "none",
  background: "#28a745",
  color: "#fff",
  cursor: "pointer",
  fontWeight: 600,
};

/*
 * ── public/pcm-processor.js ──────────────────────────────────────────
 * Save this as a SEPARATE file at public/pcm-processor.js in your React
 * app (same as your old Oilvia setup used). It queues incoming Int16 PCM
 * chunks and plays them back seamlessly, posting {type:"ended"} back to
 * the main thread once the queue actually drains after an "end" signal.
 *
 * class PCMProcessor extends AudioWorkletProcessor {
 *   constructor() {
 *     super();
 *     this.queue = [];
 *     this.readOffset = 0;
 *     this.ended = false;
 *     this.port.onmessage = (e) => {
 *       const msg = e.data;
 *       if (msg instanceof ArrayBuffer) {
 *         const int16 = new Int16Array(msg);
 *         const float32 = new Float32Array(int16.length);
 *         for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
 *         this.queue.push(float32);
 *         this.ended = false;
 *       } else if (msg?.type === "end") {
 *         this.ended = true;
 *       } else if (msg?.type === "clear") {
 *         this.queue = [];
 *         this.readOffset = 0;
 *         this.ended = false;
 *       }
 *     };
 *   }
 *
 *   process(inputs, outputs) {
 *     const output = outputs[0][0];
 *     let i = 0;
 *     while (i < output.length) {
 *       if (this.queue.length === 0) {
 *         output.fill(0, i);
 *         if (this.ended) {
 *           this.port.postMessage({ type: "ended" });
 *           this.ended = false;
 *         }
 *         break;
 *       }
 *       const chunk = this.queue[0];
 *       const remaining = chunk.length - this.readOffset;
 *       const toCopy = Math.min(remaining, output.length - i);
 *       output.set(chunk.subarray(this.readOffset, this.readOffset + toCopy), i);
 *       i += toCopy;
 *       this.readOffset += toCopy;
 *       if (this.readOffset >= chunk.length) {
 *         this.queue.shift();
 *         this.readOffset = 0;
 *       }
 *     }
 *     return true;
 *   }
 * }
 * registerProcessor("pcm-processor", PCMProcessor);
 */
