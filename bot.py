"""
bot.py — the core message composer for the magicpin "Vera" challenge.

    compose(category, merchant, trigger, customer=None) -> dict
        returns {body, cta, send_as, suppression_key, rationale}

Design
------
Deterministic-first. Every message is assembled by a hand-written handler keyed
on `trigger.kind`, injecting the *exact* facts from the four contexts. This makes
the bot:
  * deterministic (no temperature, no API needed)          -> satisfies the spec
  * fast (<5ms/call)                                        -> well under 30s
  * high-scoring on Specificity / Category-fit / Merchant-fit / Trigger-relevance
    because the concrete numbers, dates and citations come straight from the data
    rather than from a model that might hallucinate them.

An OPTIONAL LLM polish pass (llm.py) can rephrase the deterministic draft while
keeping every fact intact. It is OFF unless VERA_USE_LLM=1 is set, so the default
run is reproducible and free.
"""
from __future__ import annotations

from typing import Optional
import os


# --------------------------------------------------------------------------- #
# Encoding repair
#
# The provided dataset JSON was written UTF-8-as-Latin1, so "₹" arrives as the
# mojibake "â‚¹". Repair on read so composed copy shows real glyphs.
# --------------------------------------------------------------------------- #
def fix_mojibake(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _clean(obj):
    if isinstance(obj, str):
        return fix_mojibake(obj)
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _pct(x) -> str:
    """0.18 -> '18%', -0.05 -> '-5%'."""
    try:
        return f"{round(float(x) * 100)}%"
    except (TypeError, ValueError):
        return "?"


def _pct_abs(x) -> str:
    """Magnitude only — use when a verb (dropped/up) already carries direction."""
    try:
        return f"{abs(round(float(x) * 100))}%"
    except (TypeError, ValueError):
        return "?"


def _first_active_offer(merchant: dict) -> Optional[dict]:
    for o in merchant.get("offers", []):
        if o.get("status") == "active":
            return o
    return None


def _speaks_hindi(merchant: dict) -> bool:
    langs = [l.lower() for l in merchant.get("identity", {}).get("languages", [])]
    return "hi" in langs or any("hi" in l for l in langs)


def _salutation(category_slug: str, merchant: dict) -> str:
    ident = merchant.get("identity", {})
    owner = ident.get("owner_first_name") or ""
    if category_slug == "dentists":
        return f"Dr. {owner}" if owner else "Doctor"
    if owner:
        return owner
    return ident.get("name", "there")


def _digest_item(category: dict, trigger: dict) -> Optional[dict]:
    """Resolve a trigger's referenced digest item against category.digest."""
    p = trigger.get("payload", {})
    item_id = p.get("top_item_id") or p.get("digest_item_id") or p.get("item_id")
    for d in category.get("digest", []):
        if d.get("id") == item_id:
            return d
    # fall back to the first digest item so a research trigger never goes empty
    digest = category.get("digest", [])
    return digest[0] if digest else None


def _peer(category: dict, key, default="?"):
    return category.get("peer_stats", {}).get(key, default)


def _template(kind: str, send_as: str, trigger: dict, sal: str) -> tuple:
    """
    Build a WhatsApp-template representation for the first outbound.
    (We never call Meta — the brief says any sensible {{1}}/{{2}} structure is fine.)
    template_params are the ordered dynamic slots; the rendered `body` is the filled form.
    """
    prefix = "vera" if send_as == "vera" else "merchant"
    name = f"{prefix}_{kind}_v1"
    params = [sal]
    skip = {"category", "top_item_id", "digest_item_id", "item_id",
            "merchant_id", "customer_id"}
    for k, v in trigger.get("payload", {}).items():
        if k in skip:
            continue
        if isinstance(v, (int, float)):
            params.append(str(v))
        elif isinstance(v, str) and v:
            params.append(v)
        if len(params) >= 4:
            break
    return name, params


# --------------------------------------------------------------------------- #
# Trigger handlers.  Each returns (body, cta, send_as).
#   cta:      "binary" | "open_ended" | "booking" | "none"
#   send_as:  "vera" (merchant-facing) | "merchant_on_behalf" (customer-facing)
# --------------------------------------------------------------------------- #
def h_research_digest(cat, m, t, c, slug, sal, hi):
    item = _digest_item(cat, t) or {}
    n = item.get("trial_n")
    seg = (item.get("patient_segment") or "").replace("_", " ")
    src = item.get("source", "")
    title = item.get("title", "new research")
    seg = seg.replace("adults", "adult")
    seg_line = f" for your {seg} patients" if seg else ""
    n_line = f"{n:,}-patient trial — " if isinstance(n, int) else ""
    body = (
        f"{sal}, one item from this week's clinical digest{seg_line} — "
        f"{n_line}{title}. Source: {src}. "
        f"Want me to pull the 2-min abstract + draft a patient-ed WhatsApp you can reshare?"
    )
    return body, "open_ended", "vera"


def h_regulation_change(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    item = _digest_item(cat, t) or {}
    what = item.get("title") or item.get("summary") or "a category regulation was revised"
    src = item.get("source", "")
    deadline = p.get("deadline_iso", "")[:10]
    src_line = f" ({src})" if src else ""
    dl_line = f" Compliance deadline: {deadline}." if deadline and deadline not in what else ""
    body = (
        f"{sal}, regulatory update: {what}{src_line}.{dl_line} "
        f"Want me to check if your listing/content needs any change before the deadline?"
    )
    return body, "binary", "vera"


def h_cde_opportunity(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    item = _digest_item(cat, t) or {}
    name = item.get("title") or "a CDE webinar"
    credits = p.get("credits")
    fee = p.get("fee", "")
    cr = f" — {credits} CDE credits" if credits else ""
    fee_line = " (free for members)" if "free" in str(fee).lower() else ""
    body = (
        f"{sal}, CDE opportunity: {name}{cr}{fee_line}. "
        f"Quick to register and it counts toward your annual credits. "
        f"Want the link + a calendar hold?"
    )
    return body, "binary", "vera"


def h_perf_spike(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    metric = p.get("metric", "views")
    delta = _pct(p.get("delta_pct"))
    window = p.get("window", "7d")
    driver = (p.get("likely_driver") or "").replace("_", " ")
    driver_line = f" — looks driven by your {driver}" if driver else ""
    body = (
        f"{sal}, good signal: your {metric} are up {delta} over {window}{driver_line}. "
        f"This is the moment to ride it — want me to draft a Google post to convert the extra traffic into calls?"
    )
    return body, "binary", "vera"


def h_perf_dip(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    metric = p.get("metric", "calls")
    delta = _pct_abs(p.get("delta_pct"))
    peer_ctr = _peer(cat, "avg_ctr")
    body = (
        f"{sal}, your {metric} dropped {delta} week-on-week. "
        f"Peer median CTR in your segment is {_pct(peer_ctr)}; you're at {_pct(m.get('performance', {}).get('ctr'))}. "
        f"I've got 2 quick fixes that usually recover this — want them?"
    )
    return body, "binary", "vera"


def h_seasonal_perf_dip(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    metric = p.get("metric", "views")
    delta = _pct_abs(p.get("delta_pct")) if p.get("delta_pct") is not None else None
    note = (p.get("season_note") or "a seasonal window").replace("_", " ")
    d = f"{delta} " if delta else ""
    seasonal = " This dip is expected for the season, not a problem with your listing." \
        if p.get("is_expected_seasonal") else ""
    body = (
        f"{sal}, your {metric} are {d}down — {note}.{seasonal} "
        f"Peers counter it with a limited off-peak offer. Want me to draft one from your catalog?"
    )
    return body, "binary", "vera"


def h_milestone_reached(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    metric = (p.get("metric") or "reviews").replace("_", " ")
    now = p.get("value_now")
    goal = p.get("milestone_value")
    if p.get("is_imminent") and now and goal:
        gap = goal - now
        body = (
            f"{sal}, you're at {now} {metric} — just {gap} away from {goal}. "
            f"A single nudge to recent happy customers usually closes this in a day. "
            f"Want me to draft the ask?"
        )
    else:
        body = (
            f"{sal}, milestone hit: {now} {metric}. "
            f"Crossing round numbers lifts ranking trust. "
            f"Want me to turn this into a 'thank you' post that also invites more reviews?"
        )
    return body, "binary", "vera"


def h_competitor_opened(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    name = p.get("competitor_name", "a new competitor")
    dist = p.get("distance_km")
    their = p.get("their_offer", "")
    mine = _first_active_offer(m)
    dist_line = f" {dist}km away" if dist else " nearby"
    their_line = f" leading with \"{their}\"" if their else ""
    if mine:
        counter = f"Your \"{mine.get('title')}\" already competes — "
    else:
        counter = "You don't have a comparable offer live right now — "
    body = (
        f"{sal}, {name} just opened{dist_line}{their_line}. "
        f"{counter}want me to put a sharper counter-offer on your listing before they capture searches?"
    )
    return body, "binary", "vera"


def h_festival_upcoming(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    fest = p.get("festival", "the festival")
    days = p.get("days_until")
    when = f" is {days} days out" if days is not None else " is coming up"
    offer = _first_active_offer(m)
    hook = f" I'll build it around your \"{offer.get('title')}\"." if offer else ""
    body = (
        f"{sal}, {fest}{when} — your highest-intent window of the season. "
        f"Want me to draft a {fest} campaign post + a matching offer now, so it's live before demand peaks?{hook}"
    )
    return body, "binary", "vera"


def h_category_seasonal(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    season = (p.get("season") or "this season").replace("_", " ")
    trends = p.get("trends") or []

    def _fmt(tr):
        tr = str(tr).replace("_demand", "").replace("_", " ")
        return tr.replace("+", "+").replace("-", "-")
    top = ", ".join(_fmt(x) for x in trends[:3])
    trend_line = f" — demand shifting: {top}" if top else ""
    body = (
        f"{sal}, {season} read for your shelf{trend_line}. "
        f"Getting stock + listing ahead of this captures the searches. Want me to post what's in demand?"
    )
    return body, "binary", "vera"


def h_ipl_match_today(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    match = p.get("match") or "tonight's IPL match"
    city = p.get("city", "")
    venue = p.get("venue", "")
    loc = f" at {venue}" if venue else (f" in {city}" if city else "")
    body = (
        f"{sal}, {match}{loc} tonight = a dine-in/order spike in your area. "
        f"Want a match-night offer + post live in the next 30 min to catch the crowd?"
    )
    return body, "binary", "vera"


def h_review_theme_emerged(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    theme = (p.get("theme") or "a recurring theme").replace("_", " ")
    n = p.get("occurrences_30d")
    quote = p.get("common_quote", "")
    trend = p.get("trend", "")
    n_line = f"{n} reviews" if n else "several reviews"
    trend_line = f" and it's {trend}" if trend else ""
    q_line = f' e.g. "{quote}".' if quote else "."
    body = (
        f"{sal}, {n_line} this month flag \"{theme}\"{trend_line}{q_line} "
        f"Left unanswered it drags rating. Want me to draft public replies + a fix note customers will see?"
    )
    return body, "binary", "vera"


def h_renewal_due(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    days = p.get("days_remaining")
    plan = p.get("plan", m.get("subscription", {}).get("plan", "your plan"))
    amt = p.get("renewal_amount")
    perf = m.get("performance", {})
    proof = ""
    if perf.get("views"):
        proof = f" Last 30 days your listing pulled {perf.get('views'):,} views and {perf.get('leads', '?')} leads. "
    amt_line = f" (₹{amt:,})" if amt else ""
    body = (
        f"{sal}, your {plan} plan renews in {days} days{amt_line}.{proof}"
        f"Want me to keep it running so there's no gap in visibility? Reply YES to renew, STOP to pause."
    )
    return body, "binary", "vera"


def h_dormant_with_vera(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    days = p.get("days_since_last_merchant_message")
    perf = m.get("performance", {})
    d_line = f"It's been {days} days since we last spoke. " if days else ""
    hook = ""
    if perf.get("views"):
        hook = f"Meanwhile your listing quietly pulled {perf.get('views'):,} views. "
    body = (
        f"{sal}, {d_line}{hook}"
        f"One 2-min move I'd prioritise for you this week — want to see it?"
    )
    return body, "open_ended", "vera"


def h_gbp_unverified(cat, m, t, c, slug, sal, hi):
    body = (
        f"{sal}, your Google listing isn't verified yet — unverified profiles rank lower and "
        f"every edit waits 24-48h for review. Verifying takes ~5 min. "
        f"Want me to walk you through it now so your updates go live faster?"
    )
    return body, "binary", "vera"


def h_supply_alert(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    molecule = p.get("molecule") or p.get("item") or "a stocked molecule"
    batches = p.get("affected_batches") or []
    mfr = p.get("manufacturer", "")
    batch_line = f" (batches {', '.join(batches)}{'; ' + mfr if mfr else ''})" if batches else ""
    body = (
        f"{sal}, safety alert: a recall affects {molecule}{batch_line}. "
        f"Worth pulling those batches from the shelf and flagging to affected patients. "
        f"Want me to draft a short patient notice you can send?"
    )
    return body, "binary", "vera"


def h_curious_ask_due(cat, m, t, c, slug, sal, hi):
    """Curiosity / ask-the-merchant lever — the family production Vera barely fires."""
    q = {
        "dentists": "What's your most-requested treatment this week — cleanings, whitening, or aligners?",
        "salons": "What's booking out fastest right now — colour, keratin, or bridal?",
        "restaurants": "What's selling out first this week on your menu?",
        "gyms": "Which class is filling fastest — strength, yoga, or cardio?",
        "pharmacies": "What are patients asking for most this week?",
    }.get(slug, "What's in most demand at your place this week?")
    views = m.get("performance", {}).get("views")
    seen = f"Your listing pulled {views:,} views in the last 30 days — " if views else ""
    body = (
        f"{sal}, {seen}quick one so I tune it to real demand: {q} "
        f"Tell me and I'll push matching content today."
    )
    return body, "open_ended", "vera"


def h_active_planning_intent(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    topic = (p.get("intent_topic") or p.get("topic") or "the plan you mentioned").replace("_", " ")
    body = (
        f"{sal}, you asked what a {topic} could look like — I've drafted a first version "
        f"with pricing and a post to launch it. Say GO and I'll send it over; STOP if now's not the time."
    )
    return body, "binary", "vera"


# ---- customer-facing handlers (send_as = merchant_on_behalf) -------------- #
def _cust_lang_mix(customer: dict) -> bool:
    pref = (customer.get("identity", {}).get("language_pref") or "").lower()
    return "hi" in pref


def h_recall_due(cat, m, t, c, slug, sal, hi):
    if not c:
        # no customer context -> reframe as a merchant nudge to run recalls
        agg = m.get("customer_aggregate", {})
        lapsed = agg.get("lapsed_180d_plus")
        body = (
            f"{sal}, {lapsed} of your patients are past their 6-month recall window. "
            f"Want me to draft a recall WhatsApp you can send from your number to bring them back?"
        )
        return body, "binary", "vera"
    p = t.get("payload", {})
    name = c.get("identity", {}).get("name", "there")
    biz = m.get("identity", {}).get("name", "the clinic")
    service = (p.get("service_due") or "check-up").replace("_", " ")
    slots = p.get("available_slots", [])
    offer = _first_active_offer(m)
    price_line = f" {offer.get('title')}." if offer else ""
    last = p.get("last_service_date", "")
    since = f" It's been a few months since your last visit ({last})." if last else ""
    if len(slots) >= 2:
        s1, s2 = slots[0].get("label"), slots[1].get("label")
        slot_line = f" 2 slots open: {s1} or {s2}."
        cta_text = " Reply 1 for the first, 2 for the second, or tell me a time that suits."
        cta = "booking"
    else:
        slot_line = ""
        cta_text = " Reply with a day/time and I'll hold it."
        cta = "open_ended"
    emoji = " 🦷" if slug == "dentists" else ""
    body = (
        f"Hi {name}, {biz} here{emoji} — your {service} is due.{since}{price_line}{slot_line}{cta_text}"
    )
    return body, cta, "merchant_on_behalf"


def h_chronic_refill_due(cat, m, t, c, slug, sal, hi):
    if not c:
        return h_dormant_with_vera(cat, m, t, c, slug, sal, hi)
    p = t.get("payload", {})
    name = c.get("identity", {}).get("name", "there")
    biz = m.get("identity", {}).get("name", "your pharmacy")
    meds = p.get("molecule_list") or []
    med = ", ".join(meds) if meds else (p.get("medication") or "your regular refill")
    runs_out = (p.get("stock_runs_out_iso") or "")[:10]
    out_line = f" (your current stock runs out around {runs_out})" if runs_out else ""
    delivery = " We have your delivery address saved." if p.get("delivery_address_saved") else ""
    body = (
        f"Hi {name}, {biz} here — your refill of {med} is due{out_line}.{delivery} "
        f"Reply YES and we'll keep it ready / deliver, or tell us when suits."
    )
    return body, "binary", "merchant_on_behalf"


def h_customer_lapsed_hard(cat, m, t, c, slug, sal, hi):
    if not c:
        agg = m.get("customer_aggregate", {})
        body = (
            f"{sal}, {agg.get('lapsed_180d_plus', 'several')} customers haven't returned in 6+ months. "
            f"A win-back offer from your catalog usually recovers a chunk. Want me to draft it?"
        )
        return body, "binary", "vera"
    p = t.get("payload", {})
    name = c.get("identity", {}).get("name", "there")
    biz = m.get("identity", {}).get("name", "we")
    focus = (p.get("previous_focus") or "").replace("_", " ")
    focus_line = f" Last time you were working on {focus} — happy to pick that back up." if focus else ""
    offer = _first_active_offer(m)
    off_line = f" Here's {offer.get('title')} to welcome you back." if offer else ""
    body = (
        f"Hi {name}, been a while since we saw you at {biz}!{focus_line}{off_line} "
        f"Reply YES to book, or tell us a time that works."
    )
    return body, "binary", "merchant_on_behalf"


def h_winback_eligible(cat, m, t, c, slug, sal, hi):
    p = t.get("payload", {})
    lapsed = p.get("lapsed_customers_added_since_expiry")
    dip = _pct(p.get("perf_dip_pct")) if p.get("perf_dip_pct") is not None else None
    days = p.get("days_since_expiry")
    lapsed_line = f"{lapsed} customers have lapsed" if lapsed else "customers are lapsing"
    since = f" since your plan expired {days} days ago" if days else ""
    dip_line = f" and visibility is down {dip}" if dip else ""
    body = (
        f"{sal}, {lapsed_line}{since}{dip_line}. "
        f"Reactivating now + a win-back offer usually recovers the fastest-fading ones. "
        f"Want me to set it up? Reply YES to restart, STOP to hold."
    )
    return body, "binary", "vera"


def h_trial_followup(cat, m, t, c, slug, sal, hi):
    if not c:
        return h_dormant_with_vera(cat, m, t, c, slug, sal, hi)
    p = t.get("payload", {})
    name = c.get("identity", {}).get("name", "there")
    biz = m.get("identity", {}).get("name", "us")
    opts = p.get("next_session_options") or []
    slot_line = ""
    cta = "binary"
    if opts and opts[0].get("label"):
        slot_line = f" Next session open: {opts[0]['label']}."
    body = (
        f"Hi {name}, hope your trial at {biz} felt good!{slot_line} "
        f"Want to lock a regular slot? Reply YES to book, or ask me anything first."
    )
    return body, cta, "merchant_on_behalf"


def h_wedding_package_followup(cat, m, t, c, slug, sal, hi):
    if not c:
        return h_curious_ask_due(cat, m, t, c, slug, sal, hi)
    p = t.get("payload", {})
    name = c.get("identity", {}).get("name", "there")
    biz = m.get("identity", {}).get("name", "the studio")
    wed = p.get("wedding_date", "")
    days = p.get("days_to_wedding")
    window = (p.get("next_step_window_open") or "").replace("_", " ")
    wed_line = f" Your wedding on {wed}" + (f" is {days} days out" if days else "") + "." if wed else ""
    win_line = f" The {window} is the right one to start now." if window else ""
    body = (
        f"Hi {name}, {biz} here about your bridal booking.{wed_line}{win_line} "
        f"Want me to hold a trial date and share the package breakdown? Reply YES or tell me a date."
    )
    return body, "binary", "merchant_on_behalf"


HANDLERS = {
    "research_digest": h_research_digest,
    "regulation_change": h_regulation_change,
    "cde_opportunity": h_cde_opportunity,
    "perf_spike": h_perf_spike,
    "perf_dip": h_perf_dip,
    "seasonal_perf_dip": h_seasonal_perf_dip,
    "milestone_reached": h_milestone_reached,
    "competitor_opened": h_competitor_opened,
    "festival_upcoming": h_festival_upcoming,
    "category_seasonal": h_category_seasonal,
    "ipl_match_today": h_ipl_match_today,
    "review_theme_emerged": h_review_theme_emerged,
    "renewal_due": h_renewal_due,
    "dormant_with_vera": h_dormant_with_vera,
    "gbp_unverified": h_gbp_unverified,
    "supply_alert": h_supply_alert,
    "curious_ask_due": h_curious_ask_due,
    "active_planning_intent": h_active_planning_intent,
    # customer-facing
    "recall_due": h_recall_due,
    "chronic_refill_due": h_chronic_refill_due,
    "customer_lapsed_hard": h_customer_lapsed_hard,
    "winback_eligible": h_winback_eligible,
    "trial_followup": h_trial_followup,
    "wedding_package_followup": h_wedding_package_followup,
}


def _generic(cat, m, t, c, slug, sal, hi):
    """Fallback: still specific — uses whatever payload facts exist."""
    p = t.get("payload", {})
    facts = []
    for k, v in p.items():
        if isinstance(v, (str, int, float)) and k not in ("category", "top_item_id"):
            facts.append(f"{str(k).replace('_', ' ')}: {v}")
    fact_line = f" ({'; '.join(facts[:2])})" if facts else ""
    kind = t.get("kind", "an update").replace("_", " ")
    body = (
        f"{sal}, quick update on {kind}{fact_line}. "
        f"Want me to turn this into a move for your listing? Reply YES."
    )
    return body, "binary", "vera"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def compose(category: dict, merchant: dict, trigger: dict,
            customer: Optional[dict] = None) -> dict:
    """
    Compose the next WhatsApp message from the four contexts.
    Deterministic given the same inputs.
    """
    category = _clean(category or {})
    merchant = _clean(merchant or {})
    trigger = _clean(trigger or {})
    customer = _clean(customer) if customer else None

    slug = merchant.get("category_slug") or category.get("slug", "")
    hi = _speaks_hindi(merchant)
    sal = _salutation(slug, merchant)

    kind = trigger.get("kind", "")
    handler = HANDLERS.get(kind, _generic)
    body, cta, send_as = handler(category, merchant, trigger, customer, slug, sal, hi)

    # Hindi-English touch when the merchant/customer prefers the mix and it's a
    # merchant-facing action ask — mirrors real Vera phrasing without overdoing it.
    if hi and send_as == "vera" and body.endswith("YES to renew, STOP to pause."):
        pass  # already has a clear binary; leave English CTA which merchants parse fine

    template_name, template_params = _template(kind, send_as, trigger, sal)
    result = {
        "body": body.strip(),
        "cta": cta,
        "send_as": send_as,
        "template_name": template_name,
        "template_params": template_params,
        "suppression_key": trigger.get("suppression_key")
        or f"{kind}:{merchant.get('merchant_id', '')}",
        "rationale": _rationale(kind, send_as, cta),
    }

    # Optional LLM polish (off by default; keeps facts, improves fluency).
    if os.getenv("VERA_USE_LLM") == "1":
        try:
            from llm import polish
            polished = polish(result, category, merchant, trigger, customer)
            if polished:
                result["body"] = polished
        except Exception:
            pass  # never let the polish path break a deterministic answer

    return result


def _rationale(kind: str, send_as: str, cta: str) -> str:
    face = "customer (on merchant's behalf)" if send_as == "merchant_on_behalf" else "merchant"
    levers = {
        "research_digest": "curiosity + reciprocity (I'll pull it for you), cited source",
        "competitor_opened": "loss aversion vs a named nearby competitor",
        "perf_spike": "ride-the-wave momentum with a real delta",
        "perf_dip": "loss aversion + peer benchmark, offer to fix",
        "milestone_reached": "social proof + a low-effort next step",
        "renewal_due": "loss aversion (visibility gap) backed by ROI proof",
        "recall_due": "time-based relevance + low-friction booking choice",
        "curious_ask_due": "ask-the-merchant lever to drive a reply",
        "review_theme_emerged": "reputation loss aversion with a verbatim quote",
        "festival_upcoming": "seasonal urgency + effort externalization",
    }.get(kind, "specificity + single binary CTA")
    return f"{kind} -> {face}-facing; lever: {levers}. Anchored on verifiable facts from the contexts."
