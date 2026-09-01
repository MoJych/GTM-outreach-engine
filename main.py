"""
Domain liveness checker — a FastAPI service meant to be called from Clay
(HTTP API column) or n8n (HTTP Request node) via an ngrok tunnel.

HOW TO RUN
----------
1. Install dependencies (once):
       pip install fastapi uvicorn requests --break-system-packages

2. Pick a secret and set it as an environment variable — this is what
   protects the endpoint once ngrok makes it public.

   On Windows PowerShell (lasts for the current terminal window only):
       $env:DOMAIN_CHECKER_API_KEY = "pick-any-random-string"

   On macOS/Linux (bash/zsh):
       export DOMAIN_CHECKER_API_KEY="pick-any-random-string"

3. Start the server:
       uvicorn main:app --reload --port 8000

4. In a separate terminal, point ngrok at the same port:
       ngrok http 8000

5. Call it from Clay/n8n (or curl, to test). The domain can be sent
   EITHER as a query parameter OR as a JSON body — the endpoint accepts
   both, since Clay's HTTP API columns often send it as a query param:
       POST https://<your-ngrok-url>/check-domain?domain=example.com
       Headers: x-api-key: <the same secret from step 2>

   or:
       POST https://<your-ngrok-url>/check-domain
       Headers: x-api-key: <the same secret from step 2>
       Body (JSON): {"domain": "example.com"}

If you skip step 2 (no DOMAIN_CHECKER_API_KEY set), the key check is
skipped entirely — fine for a quick local test, not fine once the ngrok
URL is live and reachable by anyone.

PHASE 4 ADDITION — /personalize-opener
---------------------------------------
Also set an Anthropic API key (get one at console.anthropic.com, under
Settings > API Keys — needs a payment method on file, cost per call here
is a fraction of a cent on the Haiku model):

    $env:ANTHROPIC_API_KEY = "sk-ant-..."

Then call:
    POST /personalize-opener?company_name=Acme&domain=acme.com&context=Just raised a Series A
    Headers: x-api-key: <same DOMAIN_CHECKER_API_KEY as above>

PHASE 5 ADDITION — /classify-reply
-----------------------------------
Closes the loop past "email sent". No new env vars needed — reuses the
same DOMAIN_CHECKER_API_KEY and ANTHROPIC_API_KEY as above. Meant to be
called from an n8n workflow whose trigger is an Email Trigger (IMAP)
node watching the outreach inbox for replies — n8n catches the reply,
this endpoint classifies it, n8n branches on the result (update the
HubSpot deal stage, send a Calendly link, post to Slack, or do nothing
for an auto-reply). No database here on purpose — HubSpot is treated as
the system of record for lead state, not a new store built for this.

    POST /classify-reply?reply_text=Thanks, this looks interesting, can we talk Tuesday?&company_name=Acme
    Headers: x-api-key: <same DOMAIN_CHECKER_API_KEY as above>

PHASE 6 ADDITION — /handle-reply
---------------------------------
The endpoint n8n's Email Trigger (IMAP) actually calls. Resolves the
sender against HubSpot instead of hardcoding addresses in the workflow,
so the lead list can grow without editing n8n. Needs HUBSPOT_API_KEY
(a HubSpot Private App token with crm.objects.contacts.read).

PHASE 7 ADDITION — two campaigns, one service
----------------------------------------------
The original build sold outbound automation TO companies. The current
campaign is the opposite direction: getting hired or contracted BY them.
Both run through the same endpoints, switched by one field.

    /personalize-opener now takes an optional `intent`:
        intent=service  (default) — sell outbound automation to them.
                        Unchanged behaviour; the existing Clay column
                        sends no `intent` and is unaffected.
        intent=job      — pitch building/running it for them. Different
                        prompt, and quality_gate() additionally rejects
                        cover-letter cliches and any opener that starts
                        with "I"/"My"/"As" (leading with the sender is
                        the standard way this kind of email dies).

    POST /personalize-opener?company_name=Acme&domain=acme.com&intent=job&context=Hiring two SDRs and a RevOps lead this quarter

    /classify-reply and /handle-reply gained a fifth label, `referral`
    — "I'm not the right person, talk to X". Under the old four labels
    that landed in needs_info and got parked for manual review; in a
    hiring campaign it is a warm intro and one of the best outcomes
    available, so it gets its own branch.
"""

import os
import re
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import requests


class UTF8JSONResponse(JSONResponse):
    # Windows PowerShell 5.1's Invoke-RestMethod mis-decodes non-ASCII
    # characters (like an em dash "—") unless the response explicitly
    # says charset=utf-8 — the plain "application/json" that FastAPI
    # sends by default isn't enough for it to guess right.
    media_type = "application/json; charset=utf-8"


app = FastAPI(default_response_class=UTF8JSONResponse)

# Shared-secret auth: set this env var before starting the server. Any
# caller must send the same value back in the x-api-key header, or they
# get a 401. Leave it unset only for quick local testing.
API_KEY = os.environ.get("DOMAIN_CHECKER_API_KEY")

# Anthropic API key for the /personalize-opener and /classify-reply
# endpoints (Phase 4 and Phase 5).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5"

# PHASE 6 ADDITION — HubSpot lookup for /handle-reply. This is what
# replaces a hardcoded n8n filter of "only these 3 sender emails" — the
# moment a new lead is added to HubSpot, replies from them get routed
# automatically, no workflow edits needed as the list grows past 3, 30,
# or 300 contacts. Create this under HubSpot Settings > Integrations >
# Private Apps, with at least crm.objects.contacts.read scope.
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY")

# The only four buckets /classify-reply is allowed to hand back — keeping
# this a short, fixed set (rather than free-text) is what makes it safe
# for n8n to branch on downstream without another layer of parsing.
REPLY_LABELS = (
    "interested",
    "referral",
    "not_interested",
    "auto_reply",
    "needs_info",
)

# Ordered longest-first for the fallback scan in classify_reply_text().
# This matters: "interested" is a substring of "not_interested", so a
# naive scan over REPLY_LABELS in declaration order would read a clear
# rejection as interest. Scanning longest-first makes the specific label
# win over the one contained inside it.
REPLY_LABELS_BY_LENGTH = tuple(sorted(REPLY_LABELS, key=len, reverse=True))

# What n8n should do for each label. This travels in the response so the
# n8n workflow can switch on `suggested_action` directly instead of
# hardcoding the label→action mapping a second time on the n8n side.
REPLY_ACTIONS = {
    "interested": "update_hubspot_stage:meeting_requested, send_calendly_link, notify_slack",
    # Added for the job/contract campaign: "I'm not the right person,
    # talk to X" is one of the most common and most valuable replies to
    # cold outreach aimed at getting hired, and it is NOT ambiguous — it
    # is a warm intro waiting to be acted on. Previously it fell into
    # needs_info and got parked in a manual-review pile.
    "referral": "extract_referred_person, update_hubspot_stage:referred, notify_telegram",
    "not_interested": "update_hubspot_stage:closed_lost",
    "auto_reply": "no_action, retry_later",
    "needs_info": "flag_for_manual_review",
}

# A real-looking User-Agent — many sites silently block the default
# "python-requests/x.x" one with a 403.
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DomainChecker/1.0)"
}

TIMEOUT_SECONDS = 5

# Phrases that make an opener sound like every other cold email —
# reject on sight rather than let them through the quality gate.
GENERIC_PHRASES = [
    "i hope this email finds you well",
    "i came across your company",
    "i noticed your website",
    "in today's fast-paced world",
    "as a leading",
    "hope you're doing well",
]

# An opener pitching yourself for work fails differently from one
# selling a service: the cliches are cover-letter cliches, not cold-sales
# ones, and GENERIC_PHRASES catches none of them. Kept as a separate list
# so the `intent` switch on /personalize-opener applies the right one
# without loosening the checks on the other.
JOB_GENERIC_PHRASES = [
    "i am writing to express",
    "i'm writing to express",
    "i would love the opportunity",
    "i'd love the opportunity",
    "i am passionate about",
    "i'm passionate about",
    "dear hiring manager",
    "to whom it may concern",
    "i believe i would be a great fit",
    "i am reaching out to inquire",
    "proven track record",
    "i am excited about the opportunity",
    "let me introduce myself",
]

# The two campaigns /personalize-opener can write for. "service" is the
# original behaviour (selling outbound automation to a company) and stays
# the default, so the existing Clay column keeps working untouched.
OPENER_INTENTS = ("service", "job")

# If the LLM was given garbage input (e.g. a "fact" that was actually
# meta-instructions rather than a real detail about the company), it can
# reply with a refusal/explanation instead of an opener — real example:
# it wrote "I can't write this email opening because the instruction
# asks me to reference a 'personalization hook'... but that fact is
# itself a meta-description..." instead of an actual sentence. The
# length check below catches most of these (refusals run long), but
# check explicitly too, since a short refusal like "I need more
# specific information to help with this" could otherwise slip through.
REFUSAL_PATTERNS = [
    "i can't write this",
    "i cannot write this",
    "i'm unable to",
    "i am unable to",
    "as an ai",
    "i don't have enough information",
    "i need more information",
    "i need more specific information",
    "i'd need real information",
]


# The gate checks what the opener claims about THEM — the context-overlap
# rule below. It had nothing on what the opener claims about ME. Asked to
# write outreach that sells, the model fills the credibility gap with a
# track record: "for three revenue teams this year", "twice before",
# "at scale". I have no clients yet, so every one of those is false, and
# it is the kind of false that ends the conversation on the first call
# rather than in the inbox.
#
# The endpoint cannot verify a claim about the sender from in here, so it
# rejects the whole class instead. The opener has to stand on the system
# being real, which it is, and not on history that isn't.
SELF_CLAIM_PATTERNS = [
    r"\bi'?ve\s+\w+(\s+\w+){0,5}\s+(twice|three times|\d+\s+times)\b",
    r"\b(two|three|four|five|\d+)\s+(b2b\s+)?(companies|teams|clients|startups|revenue teams)\b",
    r"\bfor\s+(b2b\s+)?(companies|teams|clients)\b",
    r"\bat scale\b",
    r"\bthis year\b",
    r"\b(twice|three times)\s+before\b",
    r"\bproven\b",
    r"\bi'?ve\s+\w+(\s+\w+){0,6}\s+before\b",
]


# Matches the address inside "Display Name <email@domain.com>" — n8n's
# IMAP node sends `from` in this shape almost always, occasionally as a
# bare address with no angle brackets at all.
EMAIL_ADDR_RE = re.compile(r"<([^<>]+)>")


def extract_email(raw_from: str) -> str:
    """Pull the bare email address out of an IMAP 'From' header."""
    if not raw_from:
        return ""
    match = EMAIL_ADDR_RE.search(raw_from)
    if match:
        return match.group(1).strip().lower()
    return raw_from.strip().lower()


def find_hubspot_contact(email: str):
    """Look up a contact by email in HubSpot. Returns the contact dict
    ({'id': ..., 'properties': {...}}) if found, or None if this address
    isn't a known lead — which is how /handle-reply tells a real reply
    from an outreach target apart from unrelated inbox noise (YouTube
    receipts, newsletters, etc.), without hardcoding any addresses."""
    if not HUBSPOT_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="server is missing HUBSPOT_API_KEY — set it and restart the server",
        )
    try:
        resp = requests.post(
            "https://api.hubapi.com/crm/v3/objects/contacts/search",
            headers={
                "Authorization": f"Bearer {HUBSPOT_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": ["email", "firstname", "lastname", "company"],
                "limit": 1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"HubSpot lookup failed: {e}")

    results = data.get("results") or []
    return results[0] if results else None


def classify_reply_text(reply_text: str, company_name: str = None) -> dict:
    """Core classification logic shared by /classify-reply (manual
    testing, as before) and /handle-reply (the real IMAP-driven path).
    Returns {'classification', 'reason', 'suggested_action'}."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="server is missing ANTHROPIC_API_KEY — set it and restart the server",
        )

    prompt = (
        f"You are triaging a reply to a cold B2B outbound email"
        + (f" that was sent to someone at {company_name}" if company_name else "")
        + f'. Here is the reply, verbatim:\n\n"""\n{reply_text}\n"""\n\n'
        f"Classify it into EXACTLY ONE of these five labels:\n"
        f"- interested — wants to talk, asks a question implying interest, proposes a time\n"
        f"- referral — says they are not the right person and names or offers to "
        f"introduce someone else, or forwards the thread onward. Use this even when "
        f"the tone is lukewarm: a redirect to a named person is a referral, not "
        f"ambiguity.\n"
        f"- not_interested — a clear no, unsubscribe request, or 'not a fit'\n"
        f"- auto_reply — out-of-office, vacation responder, or an automated bounce/ack\n"
        f"- needs_info — anything else: genuinely ambiguous or unclear\n\n"
        f"Respond with ONLY the label word itself — interested, referral, "
        f"not_interested, auto_reply, or needs_info (do not write the word 'label', "
        f"substitute the real one) — then a colon, then a one-sentence reason. If the "
        f"label is referral and a person is named, put that name in the reason. "
        f"Nothing else, no text before or after.\n\n"
        f"Example: interested: asks to book a call next Tuesday"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 60,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data["content"][0]["text"].strip()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail="unexpected response shape from LLM API")

    # Parse "label: reason" — fall back to needs_info if the model didn't
    # follow the format, rather than crashing the endpoint. This mirrors
    # quality_gate()'s philosophy: never let malformed LLM output become
    # a silent wrong answer, make the uncertainty visible instead.
    label, _, reason = raw.partition(":")
    label = label.strip().lower()
    reason = reason.strip() or raw

    if label not in REPLY_LABELS:
        # Seen live on 24 Aug 2026: the model echoed the literal word
        # "LABEL" from the format instructions instead of substituting a
        # real one. The correct label was right there in the string,
        # just not before the first colon — so before giving up, scan
        # the whole response for one of the four known labels.
        lower_raw = raw.lower()
        recovered = next((l for l in REPLY_LABELS_BY_LENGTH if l in lower_raw), None)
        if recovered:
            label = recovered
            reason = f"recovered via fallback scan (model ignored the format), raw output: '{raw}'"
        else:
            label = "needs_info"
            reason = f"model did not return a recognized label, raw output: '{raw}'"

    return {
        "classification": label,
        "reason": reason,
        "suggested_action": REPLY_ACTIONS[label],
    }


async def get_field(request: Request, body: dict, name: str):
    """Read a field from the query string first, then fall back to an
    already-parsed JSON body — same flexible pattern as /check-domain,
    since different tools (Clay, n8n, curl) send data differently."""
    value = request.query_params.get(name)
    if value:
        return value
    return body.get(name) if body else None


def clean_domain(raw: str) -> str:
    """Strip any scheme/leading-or-trailing slash the caller might have
    included, so 'https://example.com/', '/example.com', and
    'example.com' all behave the same."""
    d = raw.strip()
    d = d.replace("https://", "").replace("http://", "")
    return d.strip("/")


def quality_gate(opener: str, context: str, intent: str = "service"):
    """Cheap heuristic check — no second LLM call needed. Flags generic
    phrasing, bad length, and openers that don't actually reference the
    fact they were supposed to be based on.

    `intent` selects which cliche list applies and turns on one extra
    structural rule for job/contract outreach — see below."""
    reasons = []
    lower = opener.lower()

    phrases = list(GENERIC_PHRASES)
    if intent == "job":
        phrases += JOB_GENERIC_PHRASES

    for phrase in phrases:
        if phrase in lower:
            reasons.append(f"contains generic phrase: '{phrase}'")

    for phrase in REFUSAL_PATTERNS:
        if phrase in lower:
            reasons.append(f"looks like a refusal, not an opener: contains '{phrase}'")

    word_count = len(opener.split())
    if word_count > 30:
        reasons.append(f"too long ({word_count} words, aim for under ~25)")
    if word_count < 4:
        reasons.append("too short to be a real sentence")

    context_words = {w.strip(".,!?").lower() for w in context.split() if len(w) > 4}
    opener_words = {w.strip(".,!?").lower() for w in opener.split()}
    if context_words and not (context_words & opener_words):
        reasons.append("doesn't seem to reference the given fact")

    for pattern in SELF_CLAIM_PATTERNS:
        found = re.search(pattern, lower)
        if found:
            reasons.append(
                f"claims a track record that can't be verified here: "
                f"'{found.group(0)}' — let the opener stand on the system, not on history"
            )

    if "[" in opener or "]" in opener:
        reasons.append("contains an unfilled placeholder like '[...]' — never send this as-is")

    if intent == "job":
        # A cold email written to get hired lives or dies on its first
        # three words. If it opens with the sender, it reads as an
        # application and gets skimmed; if it opens with an observation
        # about the recipient's company, it reads as someone who did the
        # work. This is the single most common failure mode in job
        # outreach and it is cheap to detect.
        first = opener.strip().split(" ")[0].strip(",.:;").lower()
        if first in ("i", "i'm", "im", "my", "as", "hi", "hello", "hey"):
            reasons.append(
                f"opens with '{first}' — leads with the sender, which reads as an "
                "application; lead with an observation about them instead"
            )

    return (len(reasons) == 0, reasons)


def probe(url: str):
    """HEAD first (cheap — no body downloaded). Some servers reject HEAD
    with 405, so fall back to GET in that case."""
    start = time.time()
    response = requests.head(
        url, timeout=TIMEOUT_SECONDS, headers=REQUEST_HEADERS, allow_redirects=True
    )
    if response.status_code == 405:
        response = requests.get(
            url, timeout=TIMEOUT_SECONDS, headers=REQUEST_HEADERS, allow_redirects=True
        )
    elapsed_ms = round((time.time() - start) * 1000)
    return response, elapsed_ms


@app.post("/check-domain")
async def check_domain(request: Request, x_api_key: str = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key header")

    # Accept the domain from a query param (?domain=example.com) — the
    # form Clay's HTTP API columns tend to use — or from a JSON body
    # ({"domain": "example.com"}), so the caller doesn't have to be
    # configured a particular way.
    raw_domain = request.query_params.get("domain")
    if not raw_domain:
        try:
            body = await request.json()
            raw_domain = body.get("domain") if body else None
        except Exception:
            raw_domain = None

    if not raw_domain:
        raise HTTPException(
            status_code=422,
            detail="missing 'domain' — pass it as ?domain=... or as JSON body {'domain': '...'}",
        )

    domain = clean_domain(raw_domain)
    last_error = None

    # Try https first (most sites), then plain http as a fallback — a
    # domain can be alive on http only, or have a broken cert on https.
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            response, elapsed_ms = probe(url)
            return {
                "domain": domain,
                "is_live": True,
                "scheme_used": scheme,
                "status_code": response.status_code,
                "final_url": response.url,
                "response_time_ms": elapsed_ms,
            }
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            continue

    return {"domain": domain, "is_live": False, "error": last_error}


@app.post("/personalize-opener")
async def personalize_opener(request: Request, x_api_key: str = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key header")

    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="server is missing ANTHROPIC_API_KEY — set it and restart the server",
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    company_name = await get_field(request, body, "company_name")
    domain = await get_field(request, body, "domain")
    context = await get_field(request, body, "context")

    # PHASE 7 — which campaign this opener is for. Defaults to "service"
    # so the existing Clay HTTP API column, which sends no `intent` at
    # all, keeps behaving exactly as before.
    intent = (await get_field(request, body, "intent")) or "service"
    intent = intent.strip().lower()
    if intent not in OPENER_INTENTS:
        raise HTTPException(
            status_code=422,
            detail=f"'intent' must be one of {list(OPENER_INTENTS)} — got '{intent}'",
        )

    if not company_name or not context:
        raise HTTPException(
            status_code=422,
            detail=(
                "need at least 'company_name' and 'context' (a real fact about the "
                "company — e.g. 'just raised a Series A', 'site loads in 300ms', "
                "'hiring 5 sales roles right now')"
            ),
        )

    who = f"{company_name}" + (f" ({domain})" if domain else "")

    shared_rules = (
        f"Never use placeholder brackets like [specific use case] or [X] — if you don't "
        f"have a concrete detail to fill a slot, leave it out entirely and write a "
        f"complete sentence with no blanks. "
        f"Return ONLY the finished sentence, nothing else."
    )

    if intent == "job":
        # The job/contract campaign. The ask is different from the service
        # campaign in a way that changes the whole sentence: the sender is
        # not offering to sell software, they are offering to build or run
        # the thing in-house. The hard rule is to open on the company, not
        # on the sender — see the matching check in quality_gate().
        prompt = (
            f"Write ONE short, specific opening line (max 25 words) for a cold email "
            f"to a founder or growth/revenue lead at {who}. "
            f"The sender is an independent engineer who builds automated outbound "
            f"systems (Clay, n8n, and his own Python/FastAPI service) and wants to be "
            f"hired or contracted to build one for them. "
            f'Base the line on this real fact about the company, referenced concretely '
            f'— do not invent anything not in the fact: "{context}". '
            f"HARD RULES: the sentence must START with the company or an observation "
            f"about them, never with 'I', 'My', 'As', or a greeting. Do not describe "
            f"the sender's skills, do not ask for a job, do not use the words "
            f"'opportunity', 'passionate', or 'reaching out'. No flattery. "
            f"NEVER claim past clients, past projects, a number of companies "
            f"served, or work done 'before', 'twice', 'three times' or 'at "
            f"scale' — the sender has no client history and any such claim is "
            f"false. See SELF_CLAIM_PATTERNS: the gate rejects these anyway. "
            f"The goal is to sound like someone who already looked closely at their "
            f"business, not like an applicant. "
            + shared_rules
        )
    else:
        prompt = (
            f"Write ONE short, specific opening line (max 25 words) for a cold outbound "
            f"email to someone at {who}"
            + f". Base it on this real fact, and reference it concretely — do not invent "
            f'anything not in the fact: "{context}". '
            f"No greeting, no \"I hope this finds you well\", no generic flattery. "
            + shared_rules
        )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        opener = data["content"][0]["text"].strip()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail="unexpected response shape from LLM API")

    passed, reasons = quality_gate(opener, context, intent)

    return {
        "company_name": company_name,
        "domain": domain,
        "intent": intent,
        "context_used": context,
        "opener": opener,
        "passed_quality_check": passed,
        "quality_notes": reasons,
    }

@app.post("/classify-reply")
async def classify_reply(request: Request, x_api_key: str = Header(default=None)):
    """Manual/testing entry point — classify a hand-typed reply_text with
    no HubSpot lookup involved. Kept as-is for the Edit Fields-driven
    testing workflow already validated in n8n. The real IMAP-driven path
    is /handle-reply below, which adds the HubSpot sender check first."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key header")

    try:
        body = await request.json()
    except Exception:
        body = {}

    reply_text = await get_field(request, body, "reply_text")
    company_name = await get_field(request, body, "company_name")

    if not reply_text:
        raise HTTPException(
            status_code=422,
            detail="need 'reply_text' — the raw text of the email reply to classify",
        )

    result = classify_reply_text(reply_text, company_name)

    return {
        "company_name": company_name,
        "reply_text": reply_text,
        "classification": result["classification"],
        "reason": result["reason"],
        "suggested_action": result["suggested_action"],
    }


@app.post("/handle-reply")
async def handle_reply(request: Request, x_api_key: str = Header(default=None)):
    """PHASE 6 — the real endpoint n8n's Email Trigger (IMAP) should call
    directly, one HTTP Request node, no per-contact configuration.

    Given the raw 'from' header and the reply body straight off the IMAP
    node, this: (1) extracts the sender's email, (2) checks HubSpot for a
    matching contact — replacing what would otherwise be a hardcoded
    n8n filter of specific addresses, so the list can grow to 300 leads
    without touching the workflow, (3) if it's a known contact, classifies
    the reply the same way /classify-reply does, (4) returns a single
    'action' field n8n can Switch on directly: 'ignore' (not a known
    contact — inbox noise), or one of the four REPLY_LABELS.

        POST /handle-reply
        Headers: x-api-key: <same DOMAIN_CHECKER_API_KEY as above>
        Body (JSON): {"from": "<the raw From header>", "reply_text": "<body>"}

    Needs HUBSPOT_API_KEY set (a HubSpot Private App token with at least
    crm.objects.contacts.read) in addition to ANTHROPIC_API_KEY.
    """
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key header")

    try:
        body = await request.json()
    except Exception:
        body = {}

    raw_from = await get_field(request, body, "from")
    reply_text = await get_field(request, body, "reply_text")

    if not raw_from:
        raise HTTPException(
            status_code=422,
            detail="need 'from' — the raw From header of the email (e.g. 'Name <a@b.com>')",
        )

    sender_email = extract_email(raw_from)
    contact = find_hubspot_contact(sender_email)

    if not contact:
        return {
            "action": "ignore",
            "sender_email": sender_email,
            "reason": "sender is not a known contact in HubSpot — not a reply to our outreach",
        }

    hubspot_contact_id = contact.get("id")
    company_name = (contact.get("properties") or {}).get("company")

    if not reply_text:
        return {
            "action": "needs_info",
            "sender_email": sender_email,
            "hubspot_contact_id": hubspot_contact_id,
            "company_name": company_name,
            "reason": "known contact replied but no text body was found on the email",
            "suggested_action": REPLY_ACTIONS["needs_info"],
        }

    result = classify_reply_text(reply_text, company_name)

    return {
        "action": result["classification"],
        "sender_email": sender_email,
        "hubspot_contact_id": hubspot_contact_id,
        "company_name": company_name,
        "reply_text": reply_text,
        "classification": result["classification"],
        "reason": result["reason"],
        "suggested_action": result["suggested_action"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
