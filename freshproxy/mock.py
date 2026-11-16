"""Offline demo / test doubles. Everything here is FICTIONAL sample data about invented products (AcmeCloud, Zorbly).
Use FRESHPROXY_MOCK=1 to try the UI without API keys. The UI shows a MOCK badge whenever this is active."""
import json, re, threading

KB = [  # (keywords, title, url, published_date, text)
    (("acmecloud", "pro plan", "price"), "AcmeCloud raises Pro plan price", "https://example.com/acmecloud-pricing", "2026-10-02",
     "As of October 2026 the AcmeCloud Pro plan costs $29 per month. It previously cost $20 per month and that price is no longer offered."),
    (("zorbly", "api", "version"), "Zorbly API v3 released", "https://example.com/zorbly-v3", "2026-09-14",
     "Zorbly API v3 is the current version. Version v2 has been deprecated and will stop working on 1 January 2027."),
    (("acmecloud", "ceo"), "AcmeCloud leadership page", "https://example.com/acmecloud-about", "2026-06-20",
     "Dana Rao is the CEO of AcmeCloud, confirmed in the June 2026 leadership update."),
]
SAMPLE = ("AcmeCloud's Pro plan costs $20 per month. The Zorbly API is currently at version v2. "
          "Dana Rao is the CEO of AcmeCloud. The sky appears blue because of Rayleigh scattering.")


class MockSearch:
    def __init__(self):
        self.stats = {"searches": 0, "cache_hits": 0, "errors": 0}
        self._l = threading.Lock()

    def search(self, query, topic="general", time_range=None, max_results=5):
        q = query.lower()
        with self._l:
            self.stats["searches"] += 1
        return [{"title": t, "url": u, "content": x, "score": 0.9, "published_date": d} for kws, t, u, d, x in KB if sum(k in q for k in kws) >= 2][:max_results]

    def snapshot(self):
        with self._l:
            return dict(self.stats)


class MockGateway:
    def __init__(self):
        self.stats = {"calls": 0, "errors": 0, "fallbacks_used": 0, "prompt_tokens": 0, "completion_tokens": 0, "models_used": {"mock/model": 0}}
        self._l = threading.Lock()

    def _tick(self):
        with self._l:
            self.stats["calls"] += 1
            self.stats["models_used"]["mock/model"] += 1

    def chat(self, messages, json_mode=False, max_tokens=900):
        self._tick()
        sysm, user = messages[0]["content"], messages[-1]["content"]
        if sysm.startswith("TASK:REWRITE"):
            ans = user.split("Corrections:")[0].replace("Answer:\n", "").strip()
            return ans.replace("$20 per month", "$29 per month [1]").replace("version v2", "version v3 [2]")
        return "(mock) This is a canned answer. " + SAMPLE

    def chat_json(self, messages, max_tokens=900):
        self._tick()
        sysm, user = messages[0]["content"], messages[-1]["content"]
        if sysm.startswith("TASK:EXTRACT"):
            claims = []
            for s in re.split(r"(?<=[.!?])\s+", user):
                if re.search(r"\$\d|version|CEO", s):
                    cat = "pricing" if "$" in s else "version" if "version" in s else "leadership"
                    claims.append({"text": s.strip(), "category": cat, "search_query": s.strip()})
            return {"claims": claims}
        if sysm.startswith("TASK:JUDGE"):
            claim = re.search(r"Claim: (.*)", user).group(1)
            res = re.findall(r"\[(\d+)\] .*?\((.*?)\) .*? — (.*)", user)
            if not res:
                return {"verdict": "unclear", "confidence": 0, "current_value": "", "reason": "no sources", "evidence": []}
            text = user.split("Results:")[1]
            nums = [int(n) for n, _, _ in res]
            if re.search(r"no longer|deprecated|previously", text) and not re.search(r"Dana Rao", claim):
                return {"verdict": "outdated", "confidence": 0.9, "current_value": re.search(r"(costs \$\d+ per month|v3 is the current)", text).group(1) if re.search(r"(costs \$\d+ per month|v3 is the current)", text) else "", "reason": "Sources show it changed.", "evidence": nums[:1]}
            return {"verdict": "supported", "confidence": 0.85, "current_value": "", "reason": "Sources confirm it.", "evidence": nums[:1]}
        return {}

    def snapshot(self):
        with self._l:
            return json.loads(json.dumps(self.stats))
