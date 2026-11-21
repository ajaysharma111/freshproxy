# FreshProxy: a freshness layer for AI answers

LLMs state outdated facts with full confidence: old prices, deprecated API versions, previous CEOs, expired laws.
**FreshProxy sits in front of any AI app, finds the claims in an answer that may have gone stale, re-checks them on the
live web, and gives every claim a freshness score with sources and a corrected answer.**

Built for the Geek Room Global AI Hackathon 2026, Problem Statement 06 (Stale-Answer Detector).
It uses both mandatory technologies, each for a distinct job:

| Technology | Job in FreshProxy |
|---|---|
| **Tavily** | Live evidence. One search per perishable claim, with dated sources. Cached so repeats cost nothing. |
| **Bifrost** | The only path to any model. Gemini is the default model through the OpenAI-compatible gateway; claim extraction, judging and rewriting use the same gateway. Provider keys, failover, budgets and logs stay in Bifrost. |

## How it works

```
 AI answer ──► 1 extract time-sensitive claims (Gemini via Bifrost; rule-based fallback if gateway is down)
           ──► 2 search each claim, most perishable first (Tavily, cached)
           ──► 3 judge claim vs numbered results (Gemini via Bifrost) -> supported / outdated / contradicted / unclear
           ──► 4 score 0..1 and label current | changed | unverified
           ──► 5 corrected answer with [n] citations (only for claims with a real source)
```

**Scoring** combines a per-category half-life (pricing 60 days, versions 120, leadership 240, regulation 365 ...) with what
live sources say and how recent they are. **Reliability rules:** a verdict that cites no real search result is discarded;
"no evidence" is labelled *unverified*, never *changed*; search or gateway failures are shown in the report, not hidden.

## What makes it more than a fact-checker

- **Live progress stream:** claims appear and resolve one by one (`/api/verify-stream`, newline-delimited JSON).
- **Your answer, highlighted:** each claim is marked green/amber/red in the original text; click one to jump to its evidence.
- **Evidence consensus and conflict detection:** every source gets a stance (supports / contradicts / neutral). Mixed evidence is shown as *sources conflict* and lowers confidence instead of being hidden. Official and documentation domains are tagged.
- **Shelf life:** each claim shows how long that kind of fact usually stays valid and a **re-check by** date.
- **Changes marked:** the corrected answer is shown as a word-level diff against the original, with `[n]` citations.
- **Gateway trace:** a table of every model call (task, model, latency, tokens, fallback or failure), showing Bifrost's role.
- **Exportable reports:** Markdown and JSON downloads.
- **Drop-in proxy:** point any OpenAI-compatible client at `/v1/chat/completions` to get a freshness report on every answer.

## Quick start (demo, no keys)

```bash
FRESHPROXY_MOCK=1 python3 -m freshproxy      # http://127.0.0.1:8787, click "Load demo sample"
```

Mock mode uses clearly fictional data (AcmeCloud, Zorbly) and shows a MOCK badge. It exists to try the UI and run tests.

## Live setup: Gemini + Bifrost + Tavily

### 1. Get API keys

- Create a Gemini API key in [Google AI Studio](https://aistudio.google.com/apikey).
- Create a Tavily API key at [Tavily](https://tavily.com).

### 2. Configure Gemini inside Bifrost

1. Start Bifrost (Docker Compose is supported below) and open its dashboard, normally `http://localhost:8080`.
2. Open **Models → Model Providers**.
3. Select **Google Gemini**. If it is not listed, use **Add New Provider**.
4. Add a key named `gemini-key`.
5. For the key value, use `env.GEMINI_API_KEY` when using the supplied Docker Compose setup, or paste the key directly in Bifrost for a standalone gateway.
6. Set **Allowed Models** to **All Models** (or explicitly allow `gemini-2.5-flash`) and set weight to `1`.
7. Save, refresh the model list, and confirm that Gemini Flash is available in **Model Catalog**.

Bifrost routes Gemini models using the `gemini/<model-id>` form. The default in this repository is
`gemini/gemini-2.5-flash`. If your Model Catalog shows a different available model ID, set that exact value in
`BIFROST_MODELS`. Do not put the Google Gemini key in FreshProxy's `BIFROST_API_KEY`; that variable is only for
authenticating FreshProxy to a protected Bifrost gateway.

### 3. Configure FreshProxy

```bash
cp .env.example .env
```

Edit `.env` and set:

- `GEMINI_API_KEY`: your Google AI Studio key (passed to the Bifrost container; Bifrost reads it as `env.GEMINI_API_KEY`).
- `TAVILY_API_KEY`: your Tavily key.
- `BIFROST_MODELS`: primary model followed by optional comma-separated fallbacks, for example
  `gemini/gemini-2.5-flash,gemini/gemini-2.5-flash-lite`.

Keep `.env` private; it is ignored by Git. Never commit real API keys.

### 4. Start the stack

With Docker Compose:

```bash
docker compose up --build
```

Open FreshProxy at `http://127.0.0.1:8787`. Bifrost is available at `http://localhost:8080`.

For a separately managed Bifrost gateway, set `BIFROST_URL` to its base URL and run:

```bash
python3 -m freshproxy
```

### 5. Verify the connection

- Open `http://127.0.0.1:8787/api/health` and confirm `models` contains the Gemini route you selected.
- Use **Ask** or verify an answer in the UI.
- Open the report's gateway trace. Successful extraction, judge and rewrite calls should show the Gemini model ID.
- If a call fails, check the Bifrost dashboard logs and confirm the Gemini key is saved and the model is allowed.

The health endpoint reports configuration, not a live upstream connectivity guarantee. A successful gateway trace is the end-to-end check.

## Docker Compose

`docker compose up --build` starts FreshProxy and Bifrost together. Compose passes `GEMINI_API_KEY` into the Bifrost
container; configure the Bifrost provider key as `env.GEMINI_API_KEY` in its dashboard. The Bifrost data directory is
persisted in `./bifrost-data`.

## API

- `POST /api/verify` `{"answer": "..." }` -> claims, scores, sources, corrected answer, Tavily/Bifrost usage.
- `POST /api/verify-stream` -> same, as live progress events ending in a `done` event.
- `POST /api/ask` `{"question": "..." }` -> asks the model through Bifrost, then verifies its answer.
- `POST /v1/chat/completions` -> **OpenAI-compatible verifying proxy.** Point any OpenAI SDK at FreshProxy; the normal
  response comes back with an extra `freshproxy` field and an `X-FreshProxy-Score` header. (`stream:true` is not supported yet.)
- `GET /api/health`.

## Configuration

| Variable | Purpose |
|---|---|
| `TAVILY_API_KEY` | Tavily search key |
| `GEMINI_API_KEY` | Gemini key passed to Bifrost; configure provider key as `env.GEMINI_API_KEY` |
| `BIFROST_URL` | Bifrost base URL (default `http://localhost:8080`) |
| `BIFROST_MODELS` | Comma-separated `provider/model` routes; first is primary, rest are fallbacks |
| `BIFROST_API_KEY` | Optional authentication key for a protected Bifrost gateway |
| `FRESHPROXY_MOCK` | Use deterministic demo mode |
| `PORT`, `HOST` | FreshProxy bind address |
| `FRESHPROXY_CACHE_TTL_S` | Tavily result cache lifetime |
| `FRESHPROXY_MAX_CLAIMS` | Maximum claims checked per answer |
| `FRESHPROXY_ASSUMED_ANSWER_AGE_DAYS` | Assumed age of an answer when scoring |

## Tests and benchmark

```bash
python3 -m unittest discover -s tests -t .
python3 benchmark/run.py
``

The benchmark uses `benchmark/dataset.json`. Run it with live keys and extend the dataset with recent changes before
quoting accuracy numbers. No accuracy figure is claimed by this repository.

## Limits

- Freshness is an estimate from web search results, not a guarantee. The judge model can still be wrong.
- Only English text and claims that a search can settle are handled well. Opinions and predictions are skipped.
- No streaming from the OpenAI-compatible proxy yet; no auth on the HTTP server (bind to localhost or put it behind your own proxy); single process.
- Live Tavily and Bifrost calls require working external credentials and network access. Unit tests should use fakes.

MIT licence. See `LICENSE`.

## 3-minute demo script

1. Run live with `GEMINI_API_KEY` configured in Bifrost and `TAVILY_API_KEY` set in FreshProxy.
2. Paste an answer mentioning prices, software versions or a current role.
3. Watch the progress stream, then click a red highlight to jump to its dated sources.
4. Show the *conflict* case (mixed sources), the diff of the corrected answer, and the gateway trace.
5. Temporarily use a deliberately invalid first model followed by a valid fallback in `BIFROST_MODELS`; the trace should show the fallback and successful completion.

## Accounts, history & API keys (v0.3)

- **Sign up / log in** (scrypt-hashed passwords, HttpOnly SameSite session cookies, JSON-only POSTs + Origin check, login rate limiting). Storage is SQLite via the stdlib (`FRESHPROXY_DB`, default `data/freshproxy.db`).
- **History**: every check by a signed-in user is saved; re-open, export or delete reports; per-user stats (checks, avg freshness, stale claims caught).
- **Per-user API key** (`fp_...`, only a hash is stored): use it as the Bearer token on `/v1/chat/completions` to get the OpenAI-compatible freshness proxy; those calls also appear in History.
- **Guests** get `FRESHPROXY_ANON_PER_HOUR` (default 5) free checks per hour per IP; accounts are unlimited.
- Set `FRESHPROXY_SECURE_COOKIE=1` behind HTTPS. On hosts with an ephemeral disk (e.g. Render free tier) the database resets on redeploy.
- Redesigned UI: landing page, score ring, claim filters, History and Developers tabs, light/dark theme.
