"""
conversation_handlers.py — multi-turn reply logic (the tiebreaker deliverable).

The judge drives POST /v1/reply with the merchant's latest message. We must
return one of:
    {"action": "send", "body": "...", "cta": "..."}
    {"action": "wait", "wait_seconds": N}
    {"action": "end", "body": "..."}   # graceful exit

Handled cases (from the brief's "open challenges"):
  1. Auto-reply detection      -> try once, then stop wasting turns
  2. Intent transition         -> "let's do it" flips pitch->action immediately
  3. Hostile / opt-out         -> apologise + end
  4. Real question / engaged   -> keep the conversation moving
  5. Knowing when to stop      -> end after repeated non-engagement

State is a plain dict the caller persists per (merchant/conversation). We also
accept a merchant-level auto-reply counter so we can detect canned replies even
when the harness spreads them across different conversation_ids.
"""
from __future__ import annotations

import re
from typing import Optional


# Canned WhatsApp Business auto-reply fingerprints (English + Hindi transliteration).
_AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"our team will (respond|get back|reach)",
    r"we will (get back|revert|respond)",
    r"automated (assistant|message|reply)",
    r"this is an auto",
    r"jaankari ke liye.*shukriya",
    r"team tak (pahuncha|pahunch)",
    r"aapki (baat|baatein).*team",
    r"main ek automated",
    r"dhanyawaad.*sampark",
]

# Explicit commitment / intent-to-act signals.
_INTENT_PATTERNS = [
    r"\blet'?s do it\b", r"\bgo ahead\b", r"\bplease proceed\b", r"\bproceed\b",
    r"\bwhat'?s next\b", r"\bwhats next\b", r"\bok+ ?,? ?lets\b", r"\byes,? do it\b",
    r"\bsounds good,? (go|do)\b", r"\bchalo\b", r"\bkaro\b", r"\bkar do\b",
    r"\bmujhe (join|jud|start) ", r"\bi want to (join|start|do)\b", r"\bsign me up\b",
    r"\bready\b",
]

# Hostile / opt-out signals.
_HOSTILE_PATTERNS = [
    r"stop messaging", r"stop texting", r"leave me alone", r"unsubscribe",
    r"\bspam\b", r"\buseless\b", r"don'?t (message|contact|text)", r"not interested",
    r"band karo", r"mat bhejo", r"pareshan mat",
]

# Affirmative-but-short (yes/haan/ok) that still means "continue with the action".
_AFFIRM_PATTERNS = [r"^\s*(yes|yeah|yep|haan|ha|ji|ok|okay|sure|done)\b"]


def _matches(text: str, patterns) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def is_auto_reply(text: str) -> bool:
    return _matches(text, _AUTO_REPLY_PATTERNS)


def is_intent(text: str) -> bool:
    return _matches(text, _INTENT_PATTERNS)


def is_hostile(text: str) -> bool:
    return _matches(text, _HOSTILE_PATTERNS)


def is_affirmative(text: str) -> bool:
    return _matches(text, _AFFIRM_PATTERNS)


def decide_reply(state: dict, merchant_message: str,
                 merchant: Optional[dict] = None) -> dict:
    """
    Core decision function. `state` is mutated to remember what's happened.

    state keys we use:
        turns:              int, how many merchant turns seen
        auto_reply_count:   int, canned replies seen for this merchant
        nudges_unanswered:  int, our nudges without real engagement
        last_bot_body:      str, so we never repeat ourselves verbatim
    """
    msg = (merchant_message or "").strip()
    state["turns"] = state.get("turns", 0) + 1

    # --- 3. Hostile / opt-out: apologise once and end. --------------------- #
    if is_hostile(msg):
        return _end(
            "Understood — I won't message again. If you ever want a hand with your "
            "listing, I'm one reply away. All the best! 🙂",
            state,
        )

    # --- 1. Auto-reply: try to break through once, then stop. -------------- #
    if is_auto_reply(msg):
        state["auto_reply_count"] = state.get("auto_reply_count", 0) + 1
        if state["auto_reply_count"] >= 2:
            return _end(
                "No problem — I'll connect with the owner/manager directly. "
                "Your business looks like it's doing well, best wishes! 🙂",
                state,
            )
        # First canned reply: one human-nudging attempt.
        return _send(
            "Samajh gayi, that looks like an auto-reply. Before it goes to the team — "
            "it's a 2-min thing only the owner can okay. Chalega? Reply YES and I'll show you.",
            state, cta="binary",
        )

    # A genuine (non-auto) merchant reply clears any auto-reply streak so state
    # can't leak across conversations on a long-lived server.
    state["auto_reply_count"] = 0

    # --- 2. Intent transition: switch straight to action mode. ------------- #
    if is_intent(msg) or is_affirmative(msg):
        return _send(
            "Perfect — on it. Draft's ready, sending it over now; reply CONFIRM and "
            "I'll publish it to your listing right away.",
            state, cta="binary",
        )

    # --- 4. A real question / substantive reply: stay engaged. ------------- #
    if "?" in msg or len(msg.split()) >= 4:
        return _send(
            "Good question — here's the short version, and I can go deeper: I'll line "
            "up exactly what your listing needs and share it for a quick yes. Want me to?",
            state, cta="binary",
        )

    # --- 5. Vague / low-signal: nudge once or twice, then bow out. ---------- #
    state["nudges_unanswered"] = state.get("nudges_unanswered", 0) + 1
    if state["nudges_unanswered"] >= 3:
        return _end(
            "I'll leave it here for now so I'm not crowding your inbox — ping me anytime "
            "and I'll pick it right back up. 🙂",
            state,
        )
    return _send(
        "No rush — just say the word and I'll get it moving whenever you're ready.",
        state, cta="open_ended",
    )


# --------------------------------------------------------------------------- #
# Response builders (with anti-repetition)
# --------------------------------------------------------------------------- #
def _send(body: str, state: dict, cta: str = "open_ended") -> dict:
    if body == state.get("last_bot_body"):
        body = body + " (Reply STOP if you'd rather I hold off.)"
    state["last_bot_body"] = body
    return {"action": "send", "body": body, "cta": cta}


def _end(body: str, state: dict) -> dict:
    state["last_bot_body"] = body
    state["ended"] = True
    return {"action": "end", "body": body}


def respond(state: dict, merchant_message: str) -> dict:
    """
    Signature from the brief (§7.4). Thin wrapper over decide_reply so the module
    can be used standalone without the HTTP server.
    """
    merchant = state.get("merchant") if isinstance(state, dict) else None
    return decide_reply(state, merchant_message, merchant)
