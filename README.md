# Domain Liveness Checker & AI Opener Personalizer

A small Python service, built while learning the GTM engineering stack (Clay + n8n + Python), that does two things: checks whether a company's website is actually live and how fast it responds, and generates a personalized cold-outreach opening line from a real fact about the company, gated by a quality check before it's usable.

## Why this exists

Before spending time on personalized outbound to a company, it's worth knowing whether their site is even reachable. Dead or broken sites are a weak signal about a company (stale, defunct, migrating) and a waste of outreach effort. This service does an HTTP check against a domain and reports back whether it's live, what status code it returned, the final URL after redirects, and how long it took to respond.

## How it fits into the GTM stack

```
Clay table (list of company domains)
        │  HTTP API column → POST /check-domain?domain=...
        ▼
FastAPI service (this repo), reachable during development via an ngrok tunnel
        │  is_live / status_code / final_url / response_time_ms
        ▼
back into the Clay table as new columns, feeding lead scoring / personalization
```

The same endpoint also works from an n8n HTTP Request node, so it drops into either half of the stack.

## Stack

- **Python 3 / FastAPI** — the service itself
- **requests** — makes the outbound HTTP probe against each domain
- **ngrok** — tunnels the local dev server to a public URL so Clay/n8n can reach it while it's still being built
- Hosting: currently running locally via ngrok during development; move to permanent hosting (Railway, Render, or a small VPS) before relying on this long-term — a free ngrok URL changes every restart

## Endpoint

`POST /check-domain`

Accepts the domain either as a query parameter or a JSON body (both are supported — Clay's HTTP API columns tend to send it as a query param):

```
POST /check-domain?domain=example.com
```
or
```
POST /check-domain
Body: {"domain": "example.com"}
```

Requires a header `x-api-key` matching the `DOMAIN_CHECKER_API_KEY` environment variable set on the server — this is what stops a random stranger from hitting the endpoint once ngrok makes it public.

**Response (site is live):**
```json
{
  "domain": "example.com",
  "is_live": true,
  "scheme_used": "https",
  "status_code": 200,
  "final_url": "https://example.com/",
  "response_time_ms": 312
}
```

**Response (site is not reachable):**
```json
{
  "domain": "example.com",
  "is_live": false,
  "error": "..."
}
```

## Endpoint: /personalize-opener

`POST /personalize-opener`

Takes a company name and a real fact about that company, calls Claude (Anthropic API) to write one specific opening line for a cold outbound email, then runs it through a quality gate before returning it.

```
POST /personalize-opener?company_name=Acme&domain=acme.com&context=Just raised a Series A
Headers: x-api-key: <same as above>
```

Requires `ANTHROPIC_API_KEY` set on the server (console.anthropic.com → Settings → API Keys) in addition to the `x-api-key` header.

**Response:**
```json
{
  "company_name": "Acme",
  "domain": "acme.com",
  "context_used": "Just raised a Series A",
  "opener": "Congrats on the Series A — curious how you're thinking about scaling outbound with the new headcount.",
  "passed_quality_check": true,
  "quality_notes": []
}
```

The quality gate is a cheap heuristic check, not a second LLM call — it rejects an opener that: uses a generic cold-email phrase ("I hope this email finds you well", "I noticed your website", etc.), is too long or too short to read as a real sentence, doesn't actually reference any word from the given fact, or contains an unfilled placeholder like `[specific use case]`. When it fails, `quality_notes` lists exactly why — the point is nothing goes out the door without a stated reason it's fine to send.

## Running locally

1. Install dependencies:
   ```
   pip install fastapi uvicorn requests --break-system-packages
   ```
2. Set the API key (PowerShell):
   ```powershell
   $env:DOMAIN_CHECKER_API_KEY = "pick-any-random-string"
   ```
3. Start the server:
   ```
   uvicorn main:app --reload --port 8000
   ```
4. In a separate terminal, expose it publicly:
   ```
   ngrok http 8000
   ```
5. Point Clay's HTTP API column (or n8n's HTTP Request node) at `https://<ngrok-url>/check-domain`, method POST, header `x-api-key: <same value as step 2>`.

## What I learned building this

- **Query param vs. JSON body.** Clay's HTTP API column sends the domain as a URL query parameter (`?domain=example.com`), not a JSON body. The first version of this service only accepted a JSON body, which caused every real request from Clay to fail with a 422. Fixed by accepting the domain from either source.
- **Windows PowerShell isn't bash.** `export VAR=value` doesn't work in PowerShell — it's `$env:VAR = "value"`, and it only lasts for that terminal window. PowerShell's `curl` is actually an alias for `Invoke-WebRequest`, which doesn't take `-X`/`-H` the way real curl does — `Invoke-RestMethod` (or `curl.exe` explicitly) is the reliable way to test from PowerShell.
- **Simple auth matters once a local server goes public.** An ngrok tunnel makes a laptop's local server reachable by anyone with the URL. A single shared-secret header (`x-api-key`) is a five-line fix for that.
- **An LLM will happily leave a template blank in its output.** The first version of `/personalize-opener` returned lines like `"...exploring integrations with [specific use case]."` — a literal unfilled placeholder, which would look broken sent to a real prospect. Fixed two ways: told the prompt explicitly never to use placeholder brackets, and added a check in the quality gate that rejects any opener containing `[` or `]` as a backstop in case the prompt fix isn't enough.
- **"It looks broken" isn't always a code bug.** A response with a real em dash (`—`) showed up in PowerShell as `â` even after retrying. The JSON itself was fine — Windows PowerShell 5.1's `Invoke-RestMethod` mis-decodes non-ASCII characters unless the response explicitly declares `charset=utf-8`, which FastAPI doesn't send by default. Fixed on the server side (a custom response class that always sets `application/json; charset=utf-8`) rather than relying on every client to configure itself correctly.

## n8n workflow

The same endpoint also works from n8n, not just Clay. Workflow: `Manual Trigger → HTTP Request (POST /check-domain) → IF (is_live == true) → branches into "Would enter outreach sequence" / "Flag as dead — skip"`.

`n8n-workflow.png` — the working workflow, executed successfully end to end against the live ngrok endpoint.

This is the minimum useful shape for this kind of check inside an automation platform: call an external service, then branch on the result instead of treating every lead the same way.

## Screenshot

`clay-table-results.png` — the Clay table with `is_live`, `status_code`, `final_url`, and `response_time_ms` populated for a batch of real companies (Simera, Joinrs, Recrut.AI, Twine, ByteByteGo, Talentify, DeepLearning.AI, CodeChef).

## Next steps

- [x] Wire the same endpoint into an n8n HTTP Request node.
- [x] Add an AI personalization step (`/personalize-opener`) gated by a quality check.
- [ ] Wire `/personalize-opener` into the Clay table as a new HTTP API column, using a real per-company signal as `context`.
- [ ] Replace the placeholder "Would enter outreach sequence" / "Flag as dead" nodes with a real HubSpot/outreach-tool action.
- [ ] Move off ngrok to permanent hosting once the build is stable.
- [ ] Feed `is_live` / `response_time_ms` into the outreach personalization step — e.g., skip companies with a dead site, or reference site speed as a genuine observation in the opener.
