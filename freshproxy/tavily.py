"""Live web evidence via Tavily Search. Results are cached so repeated claims cost no credits."""
import hashlib, json, os, threading, time
from . import http


class Search:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stats = {"searches": 0, "cache_hits": 0, "errors": 0}
        self._lock = threading.Lock()
        self._mem = {}

    def _key(self, q, topic, time_range, n):
        return hashlib.sha1(json.dumps([q, topic, time_range, n]).encode()).hexdigest()

    def search(self, query, topic="general", time_range=None, max_results=5):
        k = self._key(query, topic, time_range, max_results)
        now = time.time()
        with self._lock:
            hit = self._mem.get(k)
            if hit and now - hit[0] < self.cfg.cache_ttl_s:
                self.stats["cache_hits"] += 1
                return hit[1]
        if not self.cfg.tavily_key:
            raise RuntimeError("TAVILY_API_KEY is not set")
        body = {"query": query[:380], "search_depth": "basic", "max_results": max_results, "topic": topic, "include_answer": False}
        if time_range:
            body["time_range"] = time_range
        try:
            r = http.post_json(self.cfg.tavily_url, body, {"Authorization": "Bearer " + self.cfg.tavily_key}, timeout=25)
        except Exception:
            with self._lock:
                self.stats["errors"] += 1
            raise
        out = [{"title": x.get("title", ""), "url": x.get("url", ""), "content": (x.get("content") or "")[:700],
                "score": x.get("score"), "published_date": x.get("published_date") or x.get("published")} for x in (r.get("results") or [])]
        with self._lock:
            self.stats["searches"] += 1
            self._mem[k] = (now, out)
            if len(self._mem) > 500:
                self._mem.pop(next(iter(self._mem)))
        return out

    def snapshot(self):
        with self._lock:
            return dict(self.stats)
