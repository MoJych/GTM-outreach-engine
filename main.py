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
"""

import os
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

# Anthropic API key for the /personalize-opener endpoint (Phase 4).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5"

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


def quality_gate(opener: str, context: str):
    """Cheap heuristic check — no second LLM call needed. Flags generic
    phrasing, bad length, and openers that don't actually reference the
    fact they were supposed to be based on."""
    reasons = []
    lower = opener.lower()

    for phrase in GENERIC_PHRASES:
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

    if "[" in opener or "]" in opener:
        reasons.append("contains an unfilled placeholder like '[...]' — never send this as-is")

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

    if not company_name or not context:
        raise HTTPException(
            status_code=422,
            detail=(
                "need at least 'company_name' and 'context' (a real fact about the "
                "company — e.g. 'just raised a Series A', 'site loads in 300ms', "
                "'hiring 5 sales roles right now')"
            ),
        )

    prompt = (
        f"Write ONE short, specific opening line (max 25 words) for a cold outbound "
        f"email to someone at {company_name}"
        + (f" ({domain})" if domain else "")
        + f". Base it on this real fact, and reference it concretely — do not invent "
        f'anything not in the fact: "{context}". '
        f"No greeting, no \"I hope this finds you well\", no generic flattery. "
        f"Never use placeholder brackets like [specific use case] or [X] — if you don't "
        f"have a concrete detail to fill a slot, leave it out entirely and write a "
        f"complete sentence with no blanks. "
        f"Return ONLY the finished sentence, nothing else."
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

    passed, reasons = quality_gate(opener, context)

    return {
        "company_name": company_name,
        "domain": domain,
        "context_used": context,
        "opener": opener,
        "passed_quality_check": passed,
        "quality_notes": reasons,
    }

@app.get("/health")
def health():
    return {"status": "ok"}
