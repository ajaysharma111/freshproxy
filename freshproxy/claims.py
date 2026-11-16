"""Find the factual claims in an AI answer that could have gone stale."""
import re
from datetime import date
from .scoring import CATEGORIES

EXTRACT_SYSTEM = """TASK:EXTRACT
You find time-sensitive factual claims in an AI-generated answer. Return ONLY JSON:
{"claims":[{"text": "<the claim, self-contained, quoted or lightly normalised>",
            "category": "pricing|version|leadership|product|regulation|statistic|event|other",
            "search_query": "<a short web search query that would reveal whether this is still true today>"}]}
Rules: include only concrete, checkable statements (prices, plan limits, API/software versions, who holds a role,
laws and deadlines, product availability, statistics, 'latest/current' claims). Skip opinions, definitions and facts
that cannot change. Use category 'other' only if nothing else fits. Maximum %d claims."""

_PATTERNS = [
    ("pricing", r"[$€£₹]\s?\d|\b\d+(\.\d+)?\s?(usd|eur|inr|dollars?|per (month|year|user|token))\b|\bprice|\bpricing|\bcosts?\b|\bfree tier\b"),
    ("version", r"\bv?\d+\.\d+(\.\d+)?\b|\bversion\b|\bapi\b.*\b(deprecated|endpoint)\b|\bdeprecat|\blatest (release|version|model)\b|\bend[- ]of[- ]life\b|\beol\b"),
    ("leadership", r"\b(ceo|cto|cfo|president|prime minister|chairman|chair|director|founder|head of)\b"),
    ("regulation", r"\b(law|act|regulation|gdpr|tax|compliance|ban|legal|deadline|ruling)\b"),
    ("product", r"\b(available|launched|released|discontinued|supports?|supported|currently|retired)\b"),
    ("statistic", r"\b\d[\d,]*(\.\d+)?\s?(%|million|billion|users|customers|employees)\b|\bpopulation\b"),
    ("event", r"\b(20[12]\d)\b|\blast (year|month)\b"),
]
_NOW = re.compile(r"\b(latest|current|currently|now|today|as of|recent(ly)?|newest|still|most advanced)\b", re.I)


def heuristic_claims(answer, today=None, limit=8):
    """Offline fallback used when the gateway is unavailable: sentence splitting + keyword rules."""
    today = today or date.today()
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", answer or ""):
        s = sent.strip(" -•*\t")
        if len(s) < 12:
            continue
        cat = next((c for c, p in _PATTERNS if re.search(p, s, re.I)), None)
        if cat is None and _NOW.search(s):
            cat = "other"
        if cat is None:
            continue
        out.append({"text": s, "category": cat, "search_query": "%s %d" % (re.sub(r"\s+", " ", s)[:200], today.year)})
        if len(out) >= limit:
            break
    return out


def extract_claims(answer, gateway, limit=8, today=None):
    """Return (claims, method). Falls back to rules when the gateway cannot be reached."""
    try:
        data = gateway.chat_json([{"role": "system", "content": EXTRACT_SYSTEM % limit},
                                  {"role": "user", "content": answer[:6000]}])
        claims = []
        for c in (data.get("claims") or [])[:limit]:
            text = str(c.get("text") or "").strip()
            if not text:
                continue
            cat = str(c.get("category") or "other").lower()
            claims.append({"text": text[:400], "category": cat if cat in CATEGORIES else "other",
                           "search_query": str(c.get("search_query") or text)[:300]})
        return claims, "llm"
    except Exception:  # noqa: BLE001
        return heuristic_claims(answer, today, limit), "rules"
