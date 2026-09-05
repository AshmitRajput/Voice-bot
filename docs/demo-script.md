# RecoverAI — 5-Minute Demo Script

Timings are guides, not hard stops — the Voice Test section is the one to
protect time for; trim the tour sections if you're running long, never
that one.

---

## 0:00 – 0:20 — Hook (don't open on the dashboard)

Say this over your face or a blank screen, before showing any UI:

> "Most AI collections demos are a phone tree with a chatbot bolted on.
> RecoverAI is built around one rule instead: the AI never gets to decide
> what's true about a payment. It can talk, negotiate, even push back —
> but the database only changes when a real payment provider confirms it,
> never because the customer said so on a call. I'll show you why that
> matters."

## 0:20 – 1:00 — The admin console, fast

Open the Dashboard.

> "This is RecoverAI's admin console — Customers, Campaigns, Recovery
> Cases, Callbacks, all backed by a real Django API, not mock data."

Click through Customers → a Recovery Case → Campaigns in ~5 seconds each.
Don't linger — the point is "this is a real working system," not a tour
of every column.

## 1:00 – 1:30 — Personas & Voices

Open Personas.

> "Every call is driven by a persona — system prompt, opening line, tone,
> how aggressively it escalates, and which voice it speaks in."

Show one persona's system prompt briefly. Open Voices, show the
configured voice.

> "Voices and personas are decoupled on purpose — the same persona can
> speak in different languages or voices without touching its logic."

## 1:30 – 4:00 — THE CENTERPIECE: AI Voice Test

This is the section that actually proves the AI works. Don't rush it.

Open AI Voice Test.

> "This is a sandbox — it uses the exact same speech-to-text, LLM, and
> text-to-speech pipeline as a real outbound call, but it never touches a
> real customer record. It's how we iterate on a persona without risking
> real data."

Pick your persona, click **Start test call**. Wait for the opening line
to actually play out loud — let the audience hear it.

**Then have a real spoken exchange.** Suggested beats, in order, so you
show range, not just "it responds":

1. Say something like *"maine payment kar diya hai"* (I've already paid)
   — watch the transcript show your words, then the agent respond. Point
   out: *"Notice it doesn't just say 'great, confirmed' — it should
   verify, not just believe me."*
2. Say something like *"mujhe thoda time chahiye, mera situation kharab
   hai"* (I need more time, hardship) — show the agent offering a
   realistic option instead of pressuring you. This is your RAG
   moment if you loaded the hardship document — call it out:
   *"That answer came from a real policy document in the Knowledge Base,
   not the LLM guessing."*
3. Say *"mujhe abhi busy hoon, baad mein call karo"* (call back later) —
   show it acknowledging a callback request.

End the call. Show the transcript log on screen for a beat.

> "Every one of those turns gets logged. On a real call, this becomes a
> permanent recording and transcript you can review — coming up next."

## 4:00 – 4:30 — Call Recordings + Knowledge Base

Open Call Recordings (even if from seeded/earlier data) — show a
transcript + cost breakdown.

> "Every real call is recorded, transcribed turn-by-turn, and costed out
> per provider — STT, LLM, TTS, dialer — so this is operationally
> accountable, not a black box."

Open Knowledge Base, show 2-3 documents.

> "This is what grounds the agent's policy answers — add a document,
> it's indexed and retrievable within seconds."

## 4:30 – 5:00 — Close

> "The architecture bet here is simple: intent, action, and outcome are
> three separate things, and only outcome touches the database. That's
> what stops an AI agent from accidentally promising something, verifying
> something that never happened, or losing track of a real payment.
> That's RecoverAI."

---

## If something breaks live

- **Voice Test won't connect** — say "let me reconnect" once, retry
  once. If it fails twice, cut to the pre-recorded transcript screenshot
  and narrate over it rather than dead air.
- **No real outbound call** — don't apologize or explain Plivo billing on
  camera. Just don't show it. The script above never needs it.
- **Silence/no audio plays** — check your OS output device isn't muted
  before you start recording, not during.

## What NOT to do

- Don't open DevTools, the terminal, or any code on screen unless asked —
  judges are watching the product, not the implementation.
- Don't apologize for anything not built yet (Analytics, multi-user auth,
  etc.) — never mention scope you didn't ship.
- Don't let the tour sections (Dashboard/Customers/Campaigns) run past
  90 seconds combined — the Voice Test is what separates this from a
  static CRUD demo, protect its time budget above everything else.
