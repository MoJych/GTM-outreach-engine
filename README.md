# Python/FastAPI CRM Integration

A personal portfolio project connecting a Python service with Clay, n8n,
HubSpot and the Anthropic API. It checks website reachability, generates
draft opening lines, and classifies incoming messages for routing.

This is independent project work, not a paying client deployment. The
service returns data and suggested actions; it does not itself send emails
or update CRM stages. Some workflow destination actions are placeholders.

## Components

- `main.py`: FastAPI endpoints and text-processing logic.
- `safe_http.py`: HTTP checks restricted to public destinations.
- `make_openers.py`: generate drafts from a Clay CSV export.
- `make_pilot_openers.py`: process a pilot CSV, with review flags.
- `sync_hubspot.py`: create or update contacts from a CSV. This writes to the CRM unless `--dry-run` is used.
- `tests/test_service.py`: local regression tests with mocked external responses.

Case study: [CRM integration project](https://verbose-pump-a52.notion.site/Client-Acquisition-Engine-v1-GTM-Case-Study-3c2591ff7ce18082969dd93cf07edcd8).

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /check-domain` | HTTP reachability, status, final URL and request duration. |
| `POST /personalize-opener` | Draft text plus heuristic check results. |
| `POST /classify-reply` | One of five labels, explanation and suggested action. |
| `POST /handle-reply` | HubSpot sender lookup followed by classification. |
| `GET /health` | Basic process health; does not check external dependencies. |

POST endpoints accept query parameters or a JSON object. Query values take
precedence when nonempty. Input fields must be strings. Invalid JSON,
non-object bodies, invalid field types and missing required values return
422 (assuming required server credentials are configured).

All POST endpoints require the caller's `x-api-key` header. Missing server
configuration returns 503; a missing or wrong caller key returns 401.
`/health` remains public. There is no implicit authentication bypass for
local development.

## Run locally (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DOMAIN_CHECKER_API_KEY = "replace-with-a-long-random-secret"
# Only needed for LLM endpoints and CRM lookup respectively:
$env:ANTHROPIC_API_KEY = "your-anthropic-key"
$env:HUBSPOT_API_KEY = "your-hubspot-token"
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

Keep credentials in the environment, never in committed files. HubSpot
lookup needs contact read access; the separate CSV sync script also needs
contact write access. Anthropic calls can incur usage charges.

Example from another PowerShell terminal (set the same shared secret there):

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/check-domain `
  -Headers @{"x-api-key"=$env:DOMAIN_CHECKER_API_KEY} `
  -ContentType "application/json" -Body '{"domain":"example.com"}'
```

The input is a hostname, optionally with HTTP(S), a trailing slash and
port 80 or 443. Paths, credentials, query strings and other ports are
rejected. Each destination and redirect must resolve only to public
unicast addresses. The connection uses a validated numeric IP, preserving
the hostname for TLS verification. Redirects are limited to five, and
response bodies are not downloaded. HEAD falls back to GET on 405.

`is_live: true` means an HTTP response was received, including an error
status such as 404 or 500. Inspect `status_code` separately. Request duration
is not a browser page-speed measurement. DNS failure and network failure
are not proof that a company or its website no longer exists.

## Classification and draft checks

Labels: `interested`, `referral`, `not_interested`, `auto_reply`, `needs_info`.
The parser requires an exact `label: nonempty reason` response. It does not
search arbitrary prose for label substrings. Invalid output returns
`needs_info`, `parse_error: true`, and a manual-review action. Valid-format
responses have `parse_error: false`; this is not a claim of semantic accuracy.

For `/handle-reply`, unknown contacts return `action: ignore`; a known
contact with an empty message returns `action: needs_info`. These branches
do not invoke the classifier and do not carry `parse_error`.

Opening-line checks return `passed_quality_check` and `quality_notes`.
Failed drafts are still returned with flags. Downstream consumers must
decide whether to store or review them. The checks detect selected patterns
such as placeholders, length violations and some unsupported self-claims.
They do not verify facts or guarantee appropriate wording. A successful
HTTP response does not mean a draft passed validation.

HubSpot lookup establishes that the sender is a known contact, not that
the message belongs to a campaign. Referrals are not necessarily endorsements.
The current `auto_reply` category combines bounces and out-of-office replies;
review the subtype before implementing automatic retries.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use fake LLM responses and mocked network operations. They do not
send email, modify HubSpot, or spend API credits. They cover authentication,
input validation, malformed classifier output, quality flags, CSV score
parsing, public-IP pinning, private redirects, and HEAD-to-GET fallback.

## Deployment

`render.yaml` provides build/start commands and secret variable names.
The previous README recorded a deployment at
`https://gtm-outreach-engine-w652.onrender.com`. The September 7 local
fixes have not been deployed or checked against that live service.

Set all required credentials before deployment. `/health` alone cannot
confirm that the shared secret, Anthropic or HubSpot are configured.
ngrok is an optional development tunnel, not required to run the service.

## Limitations and next steps

- No measured classification accuracy, load-test results or availability guarantee.
- Heuristic text checks require human oversight.
- The HTTP checker uses the first validated DNS address; a connection failure
  does not currently try every address returned by DNS.
- No service-level contact history, suppression list or message deduplication.
- Sending remains manual; downstream workflow actions are not all implemented.
- Blocking HTTP calls in async endpoints are offloaded to the thread pool;
  this is not a substitute for capacity testing or rate limiting.
- Dependencies use version ranges; the repository does not lock an exact environment.

Next: persistence and duplicate-message protection, separate delivery-failure
handling, integration tests for completed workflow actions, and repeatable
deployment verification.
