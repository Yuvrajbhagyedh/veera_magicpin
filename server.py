"""
server.py — the HTTP surface the magicpin judge harness talks to.

Endpoints (exactly as the testing brief + judge_simulator.py expect):
    GET  /v1/healthz   -> status + contexts_loaded counts
    GET  /v1/metadata  -> team / model / approach
    POST /v1/context   -> versioned, idempotent context push (200 / 409 / 400)
    POST /v1/tick      -> proactive composition for available triggers
    POST /v1/reply     -> multi-turn reply (send / wait / end)

Run:
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot import compose, _clean
import conversation_handlers as ch

app = FastAPI(title="Vera-plus — magicpin AI Challenge bot")
_STARTED = time.time()


# --------------------------------------------------------------------------- #
# In-memory, versioned context store (persists for the life of the process).
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self):
        self.category: Dict[str, Dict] = {}
        self.merchant: Dict[str, Dict] = {}
        self.customer: Dict[str, Dict] = {}
        self.trigger: Dict[str, Dict] = {}
        self.versions: Dict[str, int] = {}          # (scope:id) -> version
        self.sent_suppression: set = set()          # dedup of proactive sends
        self.conversations: Dict[str, Dict] = {}     # conv_id -> state
        self.auto_reply_by_merchant: Dict[str, int] = {}

    def bucket(self, scope: str) -> Optional[Dict]:
        return getattr(self, scope, None) if scope in (
            "category", "merchant", "customer", "trigger") else None

    def counts(self) -> Dict[str, int]:
        return {
            "category": len(self.category),
            "merchant": len(self.merchant),
            "customer": len(self.customer),
            "trigger": len(self.trigger),
        }


STORE = Store()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# GET /  — friendly root so a browser visit isn't a bare 404
# --------------------------------------------------------------------------- #
@app.get("/")
def root():
    return {
        "service": "Vera-plus — magicpin AI Challenge bot",
        "status": "running",
        "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context",
                      "/v1/tick", "/v1/reply"],
        "interactive_docs": "/docs",
    }


# --------------------------------------------------------------------------- #
# GET /v1/healthz
# --------------------------------------------------------------------------- #
@app.get("/v1/healthz")
def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - _STARTED),
        "contexts_loaded": STORE.counts(),
    }


# --------------------------------------------------------------------------- #
# GET /v1/metadata
# --------------------------------------------------------------------------- #
@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": "Vera-plus",
        "team_members": ["Yuvraj K"],
        "model": "deterministic-composer/1.0 (+optional LLM polish)",
        "approach": "Per-trigger-kind deterministic composer injecting exact context "
                    "facts; category-voice adaptation; compulsion-lever selection; "
                    "stateful multi-turn with auto-reply/intent/hostile routing.",
        "contact_email": "balrajbhagyed@gmail.com",
        "version": "1.0.0",
        "submitted_at": _now(),
    }


# --------------------------------------------------------------------------- #
# POST /v1/context  — versioned + idempotent
# --------------------------------------------------------------------------- #
class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int = 1
    payload: Dict[str, Any] = {}
    delivered_at: Optional[str] = None


@app.post("/v1/context")
def push_context(req: ContextPush):
    bucket = STORE.bucket(req.scope)
    if bucket is None:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope",
                     "details": f"unknown scope '{req.scope}'"},
        )

    key = f"{req.scope}:{req.context_id}"
    current = STORE.versions.get(key)

    if current is not None:
        if req.version == current:
            # idempotent no-op
            return {"accepted": True, "ack_id": f"ack_{req.context_id}_{req.version}",
                    "stored_at": _now(), "idempotent": True}
        if req.version < current:
            return JSONResponse(
                status_code=409,
                content={"accepted": False, "reason": "stale_version",
                         "current_version": current},
            )

    # store the (repaired) payload; index category by slug too
    payload = _clean(req.payload)
    bucket[req.context_id] = payload
    STORE.versions[key] = req.version
    if req.scope == "category":
        slug = payload.get("slug")
        if slug and slug != req.context_id:
            STORE.category[slug] = payload
    return {"accepted": True, "ack_id": f"ack_{req.context_id}_{req.version}",
            "stored_at": _now()}


# --------------------------------------------------------------------------- #
# POST /v1/tick — bot decides what to send proactively
# --------------------------------------------------------------------------- #
class Tick(BaseModel):
    now: Optional[str] = None
    available_triggers: List[str] = []


def _resolve_category(merchant: Dict) -> Dict:
    slug = merchant.get("category_slug", "")
    return STORE.category.get(slug, {})


@app.post("/v1/tick")
def tick(req: Tick):
    actions: List[Dict] = []
    for tid in req.available_triggers:
        trigger = STORE.trigger.get(tid)
        if not trigger:
            continue

        supp = trigger.get("suppression_key", tid)
        if supp in STORE.sent_suppression:
            continue  # already sent this beat — respect dedup

        mid = trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
        merchant = STORE.merchant.get(mid, {})
        if not merchant:
            continue
        category = _resolve_category(merchant)
        cid = trigger.get("customer_id") or trigger.get("payload", {}).get("customer_id")
        customer = STORE.customer.get(cid) if cid else None

        msg = compose(category, merchant, trigger, customer)
        STORE.sent_suppression.add(supp)
        actions.append({
            "trigger_id": tid,
            "merchant_id": mid,
            "customer_id": cid,
            **msg,
        })

    return {"actions": actions, "now": req.now or _now()}


# --------------------------------------------------------------------------- #
# POST /v1/reply — multi-turn
# --------------------------------------------------------------------------- #
class Reply(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str = ""
    received_at: Optional[str] = None
    turn_number: int = 1


@app.post("/v1/reply")
def reply(req: Reply):
    state = STORE.conversations.setdefault(req.conversation_id, {})
    state["merchant"] = STORE.merchant.get(req.merchant_id or "", {})

    # Share the auto-reply counter across conversations for the same merchant,
    # because the harness sends canned replies under fresh conversation ids.
    if req.merchant_id:
        state["auto_reply_count"] = STORE.auto_reply_by_merchant.get(req.merchant_id, 0)

    result = ch.decide_reply(state, req.message, state.get("merchant"))

    if req.merchant_id:
        STORE.auto_reply_by_merchant[req.merchant_id] = state.get("auto_reply_count", 0)

    return result


# --------------------------------------------------------------------------- #
# Local dev entrypoint
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
