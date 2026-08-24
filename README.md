# Vera-plus — magicpin AI Challenge submission

A merchant-engagement bot that composes WhatsApp messages from the 4-context
framework (category / merchant / trigger / customer) and handles multi-turn
conversations, exposed over the exact HTTP contract the judge harness drives.

## Approach

**Deterministic-first composition.** Every message is built by a hand-written
handler keyed on `trigger.kind` (24 kinds) that injects the *exact* facts from
the four contexts — real numbers, dates, prices, citations, peer benchmarks.
This directly targets the rubric:

- **Specificity** — facts come from the data, never invented (`2,100-patient
  trial`, `JIDA Oct 2026 p.14`, `dropped 50% week-on-week`, `Smile Studio 1.3km`).
- **Category fit** — per-category voice: dentists get `Dr.` + clinical/peer tone
  and respect the vocab taboos; salons/restaurants/gyms/pharmacies each get their
  register. Offers use the service+price format (`Dental Cleaning @ ₹299`), never
  generic "X% off".
- **Merchant fit** — the merchant's own performance, offers, signals, owner name
  and language are used; Hindi-English mix is honored where the merchant prefers it.
- **Trigger relevance** — the message always names *why now* from the trigger payload.
- **Engagement compulsion** — each handler picks a lever (loss aversion, social
  proof, curiosity, reciprocity, effort-externalization, **ask-the-merchant** — the
  two families the brief says production Vera under-uses) and lands a **single
  binary CTA** in the last sentence.

**Why deterministic over pure-LLM:** it's reproducible (the spec requires
determinism), free, <5 ms/call (well under 30 s), and it structurally *cannot*
hallucinate a citation or a competitor — the #1 penalised anti-pattern. An
**optional LLM polish pass** (`llm.py`, enabled with `VERA_USE_LLM=1`) rewrites
for fluency while a guardrail keeps every fact and the CTA intact; it silently
falls back to the deterministic body on any failure.

**Multi-turn** (`conversation_handlers.py`): auto-reply fingerprint detection
(tries once, then exits — no burned turns), explicit-intent routing (pitch → action
mode immediately), hostile/opt-out graceful exit, anti-repetition, and a stop rule
after 3 unanswered nudges.

## Files

| File | Role |
|---|---|
| `bot.py` | `compose(category, merchant, trigger, customer)` — the core composer |
| `conversation_handlers.py` | `respond(state, msg)` — multi-turn reply logic |
| `server.py` | FastAPI app: `/v1/healthz`, `/metadata`, `/context`, `/tick`, `/reply` |
| `llm.py` | optional fact-preserving polish pass |
| `generate_submission.py` | builds `submission.jsonl` |
| `submission.jsonl` | 30 composed test-pair outputs |

## Run

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8080      # start the bot
python generate_submission.py                       # (re)build submission.jsonl
python judge_simulator.py                            # score it (set your key at top)
```

The context store is versioned + idempotent (`(context_id, version)`): re-posting
a version is a no-op, a lower version returns `409 stale_version`, a higher version
atomically replaces. `/tick` is suppression-aware so a beat isn't sent twice. The
dataset ships UTF-8-as-Latin1 (₹ arrives as `â‚¹`); the bot repairs this on read so
composed copy shows real glyphs.

## Tradeoffs

- Deterministic templates cap the *ceiling* of linguistic variety vs a strong LLM,
  but raise the *floor*: zero hallucinations, perfect fact fidelity, full
  reproducibility. The optional polish pass recovers fluency when a key is present.
- I optimised for the 5 scored dimensions over breadth of trigger kinds; unknown
  kinds fall through to a generic handler that still injects real payload facts.

## What extra context would have helped most

1. The **canonical 30 (merchant, trigger) pairs** — I synthesised a representative
   30, but matching the official set would make scoring like-for-like.
2. A **`preferred_cta_style` per category** (some verticals convert better on
   open-ended asks than binary YES/STOP).
3. **Prior-message log per merchant** at compose time (beyond `conversation_history`)
   to guarantee cross-session anti-repetition on proactive sends.
