"""
llm.py — OPTIONAL polish pass. Off unless VERA_USE_LLM=1.

It rewrites the deterministic draft for fluency WITHOUT inventing facts. If no
API key is present, anything fails, or the model tries to change the CTA shape,
we silently fall back to the deterministic body — so the bot is never worse for
turning this on, and stays deterministic when it's off.

Supported via env:
    VERA_USE_LLM=1
    VERA_LLM_PROVIDER=anthropic|openai   (default: anthropic)
    ANTHROPIC_API_KEY / OPENAI_API_KEY
    VERA_LLM_MODEL=...                    (optional override)
"""
from __future__ import annotations

import json
import os
from typing import Optional
from urllib import request as urlrequest

_SYS = (
    "You rewrite a WhatsApp message for an Indian local-business owner so it reads "
    "naturally. STRICT RULES: keep every number, date, price, name and citation "
    "exactly as given; do not add any fact that isn't already there; keep it to at "
    "most 2 short sentences plus the existing call-to-action as the LAST sentence; "
    "match the given voice; if the merchant prefers a Hindi-English mix, use a light, "
    "natural mix. Return ONLY the rewritten message text, nothing else."
)


def _anthropic(prompt: str, model: str) -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": model or "claude-3-5-sonnet-20241022",
        "max_tokens": 400,
        "temperature": 0,
        "system": _SYS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urlrequest.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "content-type": "application/json",
                 "anthropic-version": "2023-06-01"})
    resp = urlrequest.urlopen(req, timeout=25)
    return json.loads(resp.read())["content"][0]["text"].strip()


def _openai(prompt: str, model: str) -> Optional[str]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": model or "gpt-4o-mini",
        "temperature": 0,
        "max_tokens": 400,
        "messages": [{"role": "system", "content": _SYS},
                     {"role": "user", "content": prompt}],
    }).encode()
    req = urlrequest.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})
    resp = urlrequest.urlopen(req, timeout=25)
    return json.loads(resp.read())["choices"][0]["message"]["content"].strip()


def polish(result: dict, category: dict, merchant: dict,
           trigger: dict, customer: Optional[dict]) -> Optional[str]:
    provider = os.getenv("VERA_LLM_PROVIDER", "anthropic").lower()
    model = os.getenv("VERA_LLM_MODEL", "")
    voice = category.get("voice", {}).get("tone", "peer, helpful")
    langs = merchant.get("identity", {}).get("languages", [])
    prompt = (
        f"Voice/tone: {voice}. Merchant languages: {langs}.\n"
        f"Rewrite this message (keep all facts + keep the CTA last):\n\n{result['body']}"
    )
    try:
        out = _openai(prompt, model) if provider == "openai" else _anthropic(prompt, model)
    except Exception:
        return None
    if not out:
        return None
    # guardrail: reject if the model dropped the ask entirely or ballooned length
    if len(out) > len(result["body"]) * 2.2:
        return None
    return out
