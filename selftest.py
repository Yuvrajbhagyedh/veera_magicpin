"""
selftest.py — exercise a RUNNING bot with no LLM key required.

Start the server in another window first:
    uvicorn server:app --host 0.0.0.0 --port 8080

Then:
    python selftest.py

It pushes every context, ticks all triggers, prints each composed message with
quick structural checks, and runs the 3 multi-turn scenarios the judge cares about.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib import request as u, error as ue

# make sure non-ASCII (₹, emoji) prints on Windows consoles
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://127.0.0.1:8080"
DATA = Path(__file__).parent / "dataset"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = u.Request(BASE + path, data=data, method=method,
                    headers={"Content-Type": "application/json"})
    try:
        return json.loads(u.urlopen(req, timeout=15).read())
    except ue.HTTPError as e:
        return {"_http_error": e.code, **json.loads(e.read())}


def load(name):
    return json.load(open(DATA / name, encoding="utf-8"))


def main():
    # 0. health
    try:
        h = call("GET", "/v1/healthz")
    except Exception as e:
        print(f"Cannot reach the bot at {BASE} — is the server running?  ({e})")
        sys.exit(1)
    print(f"health: {h['status']}  uptime={h['uptime_seconds']}s\n")

    # 1. push categories, merchants, customers, triggers
    cats = {}
    for f in (DATA / "categories").glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        cats[d["slug"]] = d
        call("POST", "/v1/context", {"scope": "category", "context_id": d["slug"],
                                     "version": 1, "payload": d})
    for m in load("merchants_seed.json")["merchants"]:
        call("POST", "/v1/context", {"scope": "merchant", "context_id": m["merchant_id"],
                                     "version": 1, "payload": m})
    for c in load("customers_seed.json")["customers"]:
        call("POST", "/v1/context", {"scope": "customer", "context_id": c["customer_id"],
                                     "version": 1, "payload": c})
    triggers = load("triggers_seed.json")["triggers"]
    for t in triggers:
        call("POST", "/v1/context", {"scope": "trigger", "context_id": t["id"],
                                     "version": 1, "payload": t})
    counts = call("GET", "/v1/healthz")["contexts_loaded"]
    print(f"loaded: {counts}\n")

    # 2. tick over all triggers, score structurally
    res = call("POST", "/v1/tick", {"available_triggers": [t["id"] for t in triggers]})
    actions = res["actions"]
    print(f"=== {len(actions)} messages composed ===\n")

    import re
    ok = 0
    for a in actions:
        body = a["body"]
        has_num = bool(re.search(r"\d", body))
        has_cta = a["cta"] != "none"
        single_q = body.count("?") <= 1
        flags = []
        if not has_num:
            flags.append("no-number")
        if not has_cta:
            flags.append("no-cta")
        if not single_q:
            flags.append("multi-?")
        status = "OK " if not flags else "!! "
        if not flags:
            ok += 1
        print(f"[{status}] {a['trigger_id']}  ({a['send_as']}, cta={a['cta']})"
              + (f"  <{','.join(flags)}>" if flags else ""))
        print(f"      {body}\n")
    print(f"structural pass: {ok}/{len(actions)} clean\n")

    # 3. multi-turn scenarios
    mid = triggers[0]["merchant_id"]
    print("=== multi-turn ===")
    scen = [
        ("auto-reply x2", "Thank you for contacting us! Our team will respond shortly."),
        ("intent", "Ok lets do it. Whats next?"),
        ("hostile", "Stop messaging me. This is useless spam."),
    ]
    for label, msg in scen:
        if label.startswith("auto"):
            r1 = call("POST", "/v1/reply", {"conversation_id": "a1", "merchant_id": mid,
                                            "message": msg, "turn_number": 2})
            r2 = call("POST", "/v1/reply", {"conversation_id": "a2", "merchant_id": mid,
                                            "message": msg, "turn_number": 3})
            print(f"  {label}: turn1={r1['action']} -> turn2={r2['action']}  "
                  f"(expect send -> end)")
        else:
            r = call("POST", "/v1/reply", {"conversation_id": label, "merchant_id": mid,
                                           "message": msg, "turn_number": 2})
            print(f"  {label}: action={r['action']}  {r.get('body','')[:70]}")
    print("\nself-test complete.")


if __name__ == "__main__":
    main()
