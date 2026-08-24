"""
generate_submission.py — build submission.jsonl (the 30 test-pair outputs).

The brief's canonical 30 (merchant, trigger) pairs aren't shipped in the seed
data, so we construct a representative 30:
  * one line per seed trigger (25), each paired with the merchant it references
  * 5 extra cross-pairs reusing triggers on other same-category merchants,
    to cover more categories/voices.

Run:  python generate_submission.py
Out:  submission.jsonl  (30 lines: test_id, body, cta, send_as, suppression_key, rationale)
"""
from __future__ import annotations

import json
from pathlib import Path

from bot import compose, _clean

DATA = Path(__file__).parent / "dataset"


def load():
    cats = {}
    for f in (DATA / "categories").glob("*.json"):
        d = _clean(json.load(open(f, encoding="utf-8")))
        cats[d.get("slug", f.stem)] = d
    merchants = {m["merchant_id"]: _clean(m)
                 for m in json.load(open(DATA / "merchants_seed.json", encoding="utf-8"))["merchants"]}
    customers = {c["customer_id"]: _clean(c)
                 for c in json.load(open(DATA / "customers_seed.json", encoding="utf-8"))["customers"]}
    triggers = json.load(open(DATA / "triggers_seed.json", encoding="utf-8"))["triggers"]
    return cats, merchants, customers, [_clean(t) for t in triggers]


def build_pair(cats, merchants, customers, trigger, merchant_id=None):
    mid = merchant_id or trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
    merchant = merchants.get(mid, {})
    category = cats.get(merchant.get("category_slug", ""), {})
    cid = trigger.get("customer_id") or trigger.get("payload", {}).get("customer_id")
    customer = customers.get(cid) if cid else None
    out = compose(category, merchant, trigger, customer)
    out["merchant_id"] = mid
    out["trigger_id"] = trigger.get("id")
    return out


def main():
    cats, merchants, customers, triggers = load()
    rows = []

    for t in triggers:
        rows.append(build_pair(cats, merchants, customers, t))

    # 5 extra cross-pairs: reuse a few triggers on other same-category merchants.
    by_cat = {}
    for mid, m in merchants.items():
        by_cat.setdefault(m.get("category_slug"), []).append(mid)

    extras, seen = [], set()
    for t in triggers:
        if len(extras) >= 5:
            break
        if t.get("scope") == "customer":
            continue  # needs a specific customer; skip for cross-pairing
        mid0 = t.get("merchant_id")
        slug = merchants.get(mid0, {}).get("category_slug")
        for alt in by_cat.get(slug, []):
            keypair = (t["id"], alt)
            if alt != mid0 and keypair not in seen:
                extras.append(build_pair(cats, merchants, customers, t, merchant_id=alt))
                seen.add(keypair)
                break
    rows.extend(extras[:5])

    rows = rows[:30]
    with open(Path(__file__).parent / "submission.jsonl", "w", encoding="utf-8") as fh:
        for i, r in enumerate(rows, 1):
            line = {
                "test_id": f"T{i:02d}",
                "body": r["body"],
                "cta": r["cta"],
                "send_as": r["send_as"],
                "suppression_key": r["suppression_key"],
                "rationale": r["rationale"],
            }
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"Wrote submission.jsonl with {len(rows)} lines")


if __name__ == "__main__":
    main()
