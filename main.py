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
"""

import os
import time

from fastapi import FastAPI, Header, HTTPException, Request
import requests

app = FastAPI()

# Shared-secret auth: set this env var before starting the server. Any
# caller must send the same value back in the x-api-key header, or they
# get a 401. Leave it unset only for quick local testing.
API_KEY = os.environ.get("DOMAIN_CHECKER_API_KEY")

# A real-looking User-Agent — many sites silently block the default
# "python-requests/x.x" one with a 403.
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DomainChecker/1.0)"
}

TIMEOUT_SECONDS = 5


def clean_domain(raw: str) -> str:
    """Strip any scheme/leading-or-trailing slash the caller might have
    included, so 'https://example.com/', '/example.com', and
    'example.com' all behave the same."""
    d = raw.strip()
    d = d.replace("https://", "").replace("http://", "")
    return d.strip("/")


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