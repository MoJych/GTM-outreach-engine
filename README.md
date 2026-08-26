# GTM Outreach Engine

A working outbound system that takes a company from "never heard of them" to a routed, classified reply — and the Python service in this repo is the part Clay and n8n can't do on their own.

Five endpoints: check whether a company's site is alive, write a personalized cold-email opening line with an LLM and refuse to return it if it's bad, classify an inbound reply, resolve a reply's sender against the CRM, and a health check.

Built while learning the GTM engineering stack (Clay + n8n + Python), and used to run my own outbound.

---

## How it fits together

```
Clay table                                       n8n
─────────────────────────────                    ─────────────────────────────
Find Companies                                   Email Trigger (IMAP)
      │                                                │
      │ HTTP API column                                │ HTTP Request
      ▼                                                ▼
POST /check-domain          ┌──────────────┐    POST /handle-reply
POST /personalize-opener ───┤ this service ├─── POST /classify-reply
                            └──────┬───────┘           │
      │                            │                   ▼
      ▼                            ▼                Switch on `action`
back into the table          Anthropic API      ─────────────────────
      │                      HubSpot CRM        interested / referral /
      ▼                                         not_interested /
   HubSpot                                      auto_reply / needs_info
```

The service is reachable during development through an ngrok tunnel, so both Clay's HTTP API column and n8n's HTTP Request node can call it before it's deployed anywhere permanent.

---

## Endpoints

| Endpoint | Method | Auth | What it does |
|---|---|---|---|
| `/check-domain` | POST | `x-api-key` | Is the site live, what status code, what final URL after redirects, how many ms. |
| `/personalize-opener` | POST | `x-api-key` + `ANTHROPIC_API_KEY` | Writes one opening line from a real fact about the company, then runs a quality gate over it. |
| `/classify-reply` | POST | `x-api-key` + `ANTHROPIC_API_KEY` | Classifies a reply into one of five labels and returns the action to take. Manual/testing entry point. |
| `/handle-reply` | POST | `x-api-key` + `ANTHROPIC_API_KEY` + `HUBSPOT_API_KEY` | The one n8n actually calls. Resolves the sender against HubSpot first, then classifies. |
| `/health` | GET | none | Returns `{"status": "ok"}`. |

Every endpoint accepts its fields **either as query parameters or as a JSON body**, because different callers send data differently (see "What I learned" below).

### `POST /check-domain`

```
POST /check-domain?domain=example.com
Headers: x-api-key: <DOMAIN_CHECKER_API_KEY>
```

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

Not reachable → `{"domain": "...", "is_live": false, "error": "..."}`.

Tries `https` then falls back to `http`; sends `HEAD` first (no body downloaded) and falls back to `GET` when a server rejects `HEAD` with a 405. Sends a real-looking `User-Agent`, because a lot of sites return 403 to `python-requests/2.x`.

### `POST /personalize-opener`

```
POST /personalize-opener?company_name=Acme&domain=acme.com&context=Just raised a Series A
Headers: x-api-key: <DOMAIN_CHECKER_API_KEY>
```

```json
{
  "company_name": "Acme",
  "domain": "acme.com",
  "intent": "service",
  "context_used": "Just raised a Series A",
  "opener": "Congrats on the Series A — curious how you're thinking about scaling outbound with the new headcount.",
  "passed_quality_check": true,
  "quality_notes": []
}
```

**The quality gate is the point of this endpoint, not the LLM call.** It's a cheap heuristic check — deliberately *not* a second LLM call — that rejects an opener which:

- uses a cold-email cliché ("I hope this email finds you well", "I noticed your website", …)
- looks like a model refusal rather than an opener
- is too long or too short to read as a real sentence
- doesn't share a single meaningful word with the fact it was supposed to be based on
- contains an unfilled placeholder like `[specific use case]`

When it fails, `quality_notes` lists exactly which rules fired. Nothing goes out the door without a stated reason it's fine to send.

#### Two campaigns, one endpoint: `intent`

`intent=service` (the default) writes a line selling outbound automation *to* the company. `intent=job` writes a line from an engineer offering to build or run it *for* them.

```
POST /personalize-opener?company_name=Acme&intent=job&context=Hiring two SDRs and a RevOps lead this quarter
```

`intent=job` isn't just a different prompt — it turns on different quality rules, because outreach written to get hired fails differently:

- a separate cliché list for cover-letter phrasing ("I am writing to express", "proven track record", "I would love the opportunity"), none of which the cold-sales list catches
- a structural rule: reject any opener whose first word is `I` / `My` / `As` / a greeting. An email that opens with the sender reads as an application and gets skimmed; one that opens with an observation about the recipient reads as someone who did the work.

The default stays `service` so an existing Clay column that sends no `intent` keeps behaving exactly as it did.

### `POST /classify-reply` and `POST /handle-reply`

Both return one of five labels plus a `suggested_action` string that n8n can `Switch` on directly:

| Label | Meaning | Action |
|---|---|---|
| `interested` | wants to talk, asks a question implying interest, proposes a time | update stage → meeting requested, send Calendly, notify |
| `referral` | "I'm not the right person, talk to X" | extract the named person, update stage → referred, notify |
| `not_interested` | a clear no, unsubscribe, "not a fit" | update stage → closed lost |
| `auto_reply` | out-of-office, vacation responder, automated bounce | nothing, retry later |
| `needs_info` | genuinely ambiguous | flag for manual review |

`referral` exists because a redirect to a named colleague is a *warm introduction*, not ambiguity — it's one of the most valuable replies cold outreach produces, and lumping it into "needs review" throws that away.

`/handle-reply` is the one wired to n8n's IMAP trigger. It takes the raw `From` header and the body:

```
POST /handle-reply
Headers: x-api-key: <DOMAIN_CHECKER_API_KEY>
Body: {"from": "Jane Doe <jane@acme.com>", "reply_text": "..."}
```

It extracts the bare address, looks it up in HubSpot, and returns `action: "ignore"` if the sender isn't a known contact — that's how a real reply gets told apart from newsletters and receipts **without hardcoding any addresses in the workflow**. The obvious alternative is an n8n filter listing the people you emailed; that breaks the moment the list grows. This costs the same amount of code and works at 3 leads or 300.

---

## Running locally

```bash
pip install fastapi uvicorn requests
```

Environment variables:

| Variable | Needed by | Notes |
|---|---|---|
| `DOMAIN_CHECKER_API_KEY` | all except `/health` | Shared secret. If unset, the key check is **skipped entirely** — fine locally, not fine behind a public tunnel. |
| `ANTHROPIC_API_KEY` | `/personalize-opener`, `/classify-reply`, `/handle-reply` | console.anthropic.com → Settings → API Keys. Runs on Haiku; a fraction of a cent per call. |
| `HUBSPOT_API_KEY` | `/handle-reply` | HubSpot → Settings → Integrations → Private Apps, scope `crm.objects.contacts.read`. |

PowerShell sets these as `$env:NAME = "value"` (per terminal window), bash/zsh as `export NAME="value"`.

```bash
uvicorn main:app --reload --port 8000
ngrok http 8000     # separate terminal
```

Then point Clay's HTTP API column or n8n's HTTP Request node at `https://<ngrok-url>/<endpoint>`, method POST, header `x-api-key: <same value>`.

---

## What I learned building this

- **Query param vs. JSON body.** Clay's HTTP API column sends the domain as a URL query parameter (`?domain=example.com`), not a JSON body. The first version only accepted a body, so every real request from Clay failed with a 422. Fixed by accepting either — which is why there's a `get_field()` helper reading the query string first and the parsed body second.
- **Windows PowerShell isn't bash.** `export VAR=value` doesn't work — it's `$env:VAR = "value"`, and it only lasts for that terminal window. PowerShell's `curl` is an alias for `Invoke-WebRequest`, which doesn't take `-X`/`-H`; `Invoke-RestMethod` or `curl.exe` is the reliable way to test.
- **Simple auth matters the second a local server goes public.** An ngrok tunnel makes a laptop reachable by anyone with the URL. A single shared-secret header is a five-line fix.
- **An LLM will happily leave a template blank in its output.** The first version of `/personalize-opener` returned lines like `"...exploring integrations with [specific use case]."` — a literal unfilled placeholder that would look broken to a real prospect. Fixed twice over: told the prompt never to use placeholder brackets, *and* added a gate rule rejecting any opener containing `[` or `]`, as a backstop for when the prompt fix isn't enough.
- **"It looks broken" isn't always a code bug.** A response containing an em dash showed up in PowerShell as `â` even after retrying. The JSON was fine — PowerShell 5.1's `Invoke-RestMethod` mis-decodes non-ASCII unless the response explicitly declares `charset=utf-8`, which FastAPI doesn't send by default. Fixed on the server with a custom response class rather than asking every client to configure itself correctly.
- **Garbage in, refusal out — and the gate caught it live.** Running against a real batch of ~50 companies, one row's `context` turned out to be a meta-description ("reference a personalization hook based on a fact") rather than an actual fact, leaked from an upstream Clay AI column that had itself failed. The model responded with a paragraph explaining why it couldn't write an opener. `passed_quality_check` correctly came back `false` — caught on a real failure, not in theory. I then added an explicit `REFUSAL_PATTERNS` check so a *short* refusal can't slip through on word count alone.
- **Substring matching on labels is a silent, expensive bug.** When the model ignores the output format, `classify_reply_text()` falls back to scanning its raw response for a known label. That scan ran over the labels in declaration order — and `"interested"` is a substring of `"not_interested"`, so a clear rejection recovered as **interest**: the lead would have been moved to "meeting requested" and I'd have been notified of a win that didn't exist. Fixed by scanning longest label first. The lesson generalises: any time you match a fixed set of strings by containment, sort by length descending, or one of them will eat another.
- **Never let malformed LLM output become a confident wrong answer.** Both LLM endpoints degrade the same way on purpose: the classifier falls back to `needs_info` and says so in the `reason` field; the opener returns `passed_quality_check: false` with the rules that fired. Making the uncertainty visible is cheaper than debugging a system that quietly guessed.

---

## Honest status

What's real: all five endpoints work and are called from live Clay columns and a live n8n workflow. The Clay pipeline has run end to end against real companies. The reply workflow is active against a real inbox.

What isn't, yet:

- **Hosting is still an ngrok tunnel**, so the public URL changes on restart. Moving to Railway/Render is the next infrastructure job.
- **n8n's outcome branches are still `NoOp` nodes** — the workflow classifies a reply and sends me a Telegram notification, but "update HubSpot stage" and "send a Calendly link" are placeholders, not wired actions. It's detect-and-notify, not detect-and-act.
- **Sending is manual.** The pipeline generates and stores openers; a human still presses send.
- **No automated tests.** Every endpoint has been smoke-tested by hand and against real data, which is not the same thing.

## Next

- [x] Wire the endpoint into an n8n HTTP Request node
- [x] Add an AI personalization step gated by a quality check
- [x] Wire `/personalize-opener` into Clay using a real per-company signal as `context`
- [x] Close the loop past "email sent" — reply classification and CRM-aware routing
- [ ] Replace the `NoOp` branches with real HubSpot / Calendly / Slack actions
- [ ] Move off ngrok to permanent hosting
- [ ] Feed `is_live` / `response_time_ms` into personalization — skip dead sites, or reference site speed as a genuine observation
- [ ] Add tests around `quality_gate()` and the label-recovery fallback, the two places where a silent wrong answer is most expensive
