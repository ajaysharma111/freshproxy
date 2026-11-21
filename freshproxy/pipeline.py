"""answer -> claims -> live evidence (Tavily) -> judgement (model via Bifrost) -> freshness scores -> corrected answer."""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import urlparse

from . import claims as claims_mod
from . import scoring

JUDGE_SYSTEM = """TASK:JUDGE
You verify ONE claim from an AI answer against numbered web search results. Use ONLY the results given; if they do
not settle it, say unclear. Return ONLY JSON:
{"verdict": "supported|contradicted|outdated|unclear",
 "confidence": <0..1>,
 "current_value": "<what the sources say is true now, or empty>",
 "reason": "<one sentence>",
 "evidence": [<result numbers you relied on>],
 "stances": {"<result number>": "supports|contradicts|neutral"}}
'contradicted' = sources say the claim is false. 'outdated' = it was true before but sources show it changed."""

REWRITE_SYSTEM = """TASK:REWRITE
Rewrite the answer so that the listed outdated claims are corrected. Keep everything else unchanged, keep it concise,
and cite sources as [1], [2] matching the numbered sources provided. Do not add facts that are not in the corrections.
Return ONLY the rewritten answer text."""


def _evidence_text(results):
    return "\n".join("[%d] %s (%s) %s — %s" % (i + 1, r["title"][:90], r.get("published_date") or "undated", r["url"], r["content"][:350])
                     for i, r in enumerate(results))


def authority(url):
    """Cheap source-quality hint for the UI: official domains and vendor documentation."""
    h = (urlparse(url).hostname or "").lower()
    return bool(h.endswith((".gov", ".edu", ".int", ".gov.in", ".gov.uk")) or h.split(".")[0] in ("docs", "developer", "developers", "support", "help", "learn", "platform") or "github.com" in h and "/releases" in url)


def judge(claim, results, gateway):
    if not results:
        return {"verdict": "unclear", "confidence": 0.0, "current_value": "", "reason": "No live sources were found.", "evidence": [], "stances": {}, "conflict": False}
    try:
        j = gateway.chat_json([{"role": "system", "content": JUDGE_SYSTEM},
                               {"role": "user", "content": "Today: %s\nClaim: %s\n\nResults:\n%s" % (date.today().isoformat(), claim["text"], _evidence_text(results))}], max_tokens=500, task="judge")
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unclear", "confidence": 0.0, "current_value": "", "reason": "Judge unavailable: %s" % str(e)[:120], "evidence": [], "stances": {}, "conflict": False}
    verdict = str(j.get("verdict", "unclear")).lower()
    if verdict not in ("supported", "contradicted", "outdated", "unclear"):
        verdict = "unclear"
    # Anti-hallucination guard: a verdict only counts if it cites results that really exist.
    ev = sorted({int(x) for x in (j.get("evidence") or []) if str(x).isdigit() and 1 <= int(x) <= len(results)})
    if verdict != "unclear" and not ev:
        return {"verdict": "unclear", "confidence": 0.0, "current_value": "", "reason": "The judge cited no valid source, so the verdict was discarded.", "evidence": [], "stances": {}, "conflict": False}
    try:
        conf = min(max(float(j.get("confidence", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        conf = 0.5
    stances = {}
    for k, v in (j.get("stances") or {}).items():
        if str(k).isdigit() and 1 <= int(k) <= len(results) and v in ("supports", "contradicts", "neutral"):
            stances[int(k)] = v
    # Mixed evidence lowers confidence and is reported as a conflict instead of being hidden.
    conflict = "supports" in stances.values() and "contradicts" in stances.values()
    if conflict:
        conf = round(conf * 0.6, 2)
    return {"verdict": verdict, "confidence": conf, "current_value": str(j.get("current_value") or "")[:300],
            "reason": str(j.get("reason") or "")[:300], "evidence": ev, "stances": stances, "conflict": conflict}


def verify_claim(claim, deps, cfg, today):
    cat = claim["category"]
    tr = {"pricing": "year", "version": "year", "leadership": "year", "product": "year"}.get(cat)
    try:
        results = deps.search.search(claim["search_query"], topic="general", time_range=tr, max_results=5)
        err = None
    except Exception as e:  # noqa: BLE001
        results, err = [], str(e)[:160]
    j = judge(claim, results, deps.gateway) if not err else {"verdict": "unclear", "confidence": 0.0, "current_value": "", "reason": "Live search failed: " + err, "evidence": [], "stances": {}, "conflict": False}
    used = [results[i - 1] for i in j["evidence"]]
    newest = max((u.get("published_date") or "" for u in used), default="") or None
    s = scoring.score(cat, j["verdict"], j["confidence"], newest, cfg.assumed_answer_age_days, today)
    return {**claim, "verdict": j["verdict"], "confidence": round(j["confidence"], 2), "freshness": s, "label": scoring.label(s, j["verdict"]),
            "current_value": j["current_value"], "reason": j["reason"],
            "conflict": j["conflict"],
            "consensus": {"supports": sum(v == "supports" for v in j["stances"].values()), "contradicts": sum(v == "contradicts" for v in j["stances"].values()), "neutral": sum(v == "neutral" for v in j["stances"].values())},
            "shelf_life_days": scoring.HALF_LIFE_DAYS.get(cat, 1000), "recheck_by": (today + timedelta(days=scoring.HALF_LIFE_DAYS.get(cat, 1000))).isoformat(),
            "sources": [{"n": i, "title": results[i - 1]["title"], "url": results[i - 1]["url"], "date": results[i - 1].get("published_date"),
                         "stance": j["stances"].get(i), "official": authority(results[i - 1]["url"])} for i in j["evidence"]],
            "all_sources": [{"title": r["title"], "url": r["url"], "date": r.get("published_date")} for r in results]}


def correct(answer, checked, gateway):
    bad = [c for c in checked if c["label"] == "changed" and c["sources"]]
    if not bad:
        return answer, False
    srcs, notes = [], []
    for c in bad:
        for s in c["sources"]:
            if s["url"] not in [x["url"] for x in srcs]:
                srcs.append(s)
        notes.append("- Outdated: %s\n  Now: %s (%s)" % (c["text"], c["current_value"] or "see source", ", ".join("[%d]" % (1 + [x["url"] for x in srcs].index(s["url"])) for s in c["sources"])))
    src_text = "\n".join("[%d] %s — %s" % (i + 1, s["title"], s["url"]) for i, s in enumerate(srcs))
    try:
        txt = gateway.chat([{"role": "system", "content": REWRITE_SYSTEM},
                            {"role": "user", "content": "Answer:\n%s\n\nCorrections:\n%s\n\nSources:\n%s" % (answer, "\n".join(notes), src_text)}], max_tokens=1200, task="rewrite")
        return txt.strip(), True
    except Exception:  # noqa: BLE001
        return answer + "\n\nCorrections (automatic):\n" + "\n".join(notes) + "\n\nSources:\n" + src_text, True


class Deps:
    def __init__(self, gateway, search):
        self.gateway, self.search = gateway, search


def verify(answer, deps, cfg, today=None, emit=None):
    """emit(event, data) is called as work progresses (used for the live progress stream)."""
    emit = emit or (lambda *_: None)
    t0 = time.time()
    today = today or date.today()
    emit("step", {"text": "Finding time-sensitive claims…"})
    found, method = claims_mod.extract_claims(answer, deps.gateway, cfg.max_claims, today)
    # most perishable claims first, so the credit budget is spent where staleness is likeliest
    found.sort(key=lambda c: scoring.prior(c["category"], cfg.assumed_answer_age_days))
    found = found[:cfg.max_claims]
    emit("claims", {"count": len(found), "method": method, "claims": [{"text": c["text"], "category": c["category"]} for c in found]})
    checked = [None] * len(found)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(verify_claim, c, deps, cfg, today): i for i, c in enumerate(found)}
        for f in as_completed(futs):
            checked[futs[f]] = f.result()
            emit("claim", {"index": futs[f], "claim": checked[futs[f]]})
    emit("step", {"text": "Writing corrections…"})
    corrected, changed = correct(answer, checked, deps.gateway)
    return {"overall": scoring.overall([c["freshness"] for c in checked]), "extraction": method, "claims": checked,
            "corrected_answer": corrected if changed else None, "had_corrections": changed, "original_answer": answer,
            "stats": {"tavily": deps.search.snapshot(), "bifrost": deps.gateway.snapshot(), "elapsed_s": round(time.time() - t0, 2)},
            "disclaimer": "Freshness is an estimate from web search results; verify important facts yourself."}
