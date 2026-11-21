# FreshProxy: a freshness layer for AI answers

LLMs state outdated facts with full confidence: old prices, deprecated API versions, previous CEOs, expired laws.
**FreshProxy sits in front of any AI app, finds the claims in an answer that may have gone stale, re-checks them on the
live web, and gives every claim a freshness score with sources and a corrected answer.**

Built for the Geek Room Global AI Hackathon 2026, Problem Statement 06 (Stale-Answer Detector).
It uses both mandatory technologies, each for a distinct job:

| Technology | Job in FreshProxy |
|---|---|
| **Tavily** | Live evidence. One search per perishable claim, with dated sources. Cached so repeats cost nothing. |
| **Bifrost (Maxim AI)** | The only path to any model. Claim extraction, judging and rewriting are all OpenAI-style requests to the gateway, so provider keys, failover, budgets and logs live there. FreshProxy also retries down its own model list. |

## How it works
```
 AI answer ──► 1 extract time-sensitive claims (model via Bifrost; rule-based fallback if the gateway is down)
           ──► 2 search each claim, most perishable first (Tavily, cached)
           ──► 3 judge claim vs numbered results (model via Bifrost) -> supported / outdated / contradicted / unclear
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

## Live mode
1. Run a Bifrost gateway (see https://github.com/maximhq/bifrost, for example `npx -y @maximhq/bifrost` or the
   `maximhq/bifrost` Docker image) and add at least one provider key in it. Default URL: `http://localhost:8080`.
2. Get a Tavily key from https://tavily.com.
3. ```bash
   cp .env.example .env   # fill in values, then:
   export $(grep -v '^#' .env | xargs) && python3 -m freshproxy
   ```
Docker: `docker compose up` starts FreshProxy and Bifrost together (`docker-compose.yml`).
> Bifrost's exact flags, model naming (`provider/model`) and optional features change between releases. This project
> was written without access to a live gateway, so check the Bifrost docs if a request is rejected, and set
> `BIFROST_MODELS` accordingly.

## API
- `POST /api/verify` `{"answer": "..."}` -> claims, scores, sources, corrected answer, Tavily/Bifrost usage.
- `POST /api/verify-stream` -> same, as live progress events ending in a `done` event.
- `POST /api/ask` `{"question": "..."}` -> asks the model through Bifrost, then verifies its answer.
- `POST /v1/chat/completions` -> **OpenAI-compatible verifying proxy.** Point any OpenAI SDK at FreshProxy; the normal
  response comes back with an extra `freshproxy` field and an `X-FreshProxy-Score` header. (`stream:true` is not supported yet.)
- `GET /api/health`.

## Configuration
`TAVILY_API_KEY`, `BIFROST_URL`, `BIFROST_MODELS` (comma list, first = primary), `BIFROST_API_KEY` (optional),
`FRESHPROXY_MOCK`, `PORT`, `HOST`, `FRESHPROXY_CACHE_TTL_S`, `FRESHPROXY_MAX_CLAIMS`, `FRESHPROXY_ASSUMED_ANSWER_AGE_DAYS`.

## Tests and benchmark
```bash
python3 -m unittest discover -s tests -t .      # 22 tests, no network needed
python3 benchmark/run.py                         # live: accuracy / precision / recall on benchmark/dataset.json
```
The starter dataset has 12 items whose staleness is permanent history. **No accuracy figure is claimed here**; run it with
your keys and extend the dataset with recent changes before quoting numbers.

## Limits
- Freshness is an estimate from web search results, not a guarantee. The judge model can still be wrong.
- Only English text and claims that a search can settle are handled well. Opinions and predictions are skipped.
- No streaming, no auth on the HTTP server (bind to localhost or put it behind your own proxy), single process.
- Live Tavily and Bifrost calls were not exercised in the build environment (no internet); the integration is covered by
  unit tests with fakes.

MIT licence. See `LICENSE`.

## 3-minute demo script
1. Run live (`TAVILY_API_KEY` set, Bifrost up). Paste a real answer from any chatbot that mentions prices, versions or a job title.
2. Watch the progress stream, then click a red highlight to jump to its dated sources.
3. Show the *conflict* case (mixed sources), the diff of the corrected answer, and the gateway trace.
4. Stop one provider key in Bifrost (or list a bad first model in `BIFROST_MODELS`) and re-run: the trace shows the fallback and the check still completes.
5. Point an OpenAI SDK at `http://localhost:8787/v1` and show the `freshproxy` field on a normal chat response.
