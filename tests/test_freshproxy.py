import json, threading, unittest, urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from freshproxy import bifrost, claims, http as fphttp, pipeline, scoring
from freshproxy.config import Config
from freshproxy.mock import MockGateway, MockSearch, SAMPLE
from freshproxy.pipeline import Deps
from freshproxy.server import App, build
from freshproxy.tavily import Search

TODAY = date(2026, 11, 16)


class Scoring(unittest.TestCase):
    def test_labels_and_ordering(self):
        sup = scoring.score("pricing", "supported", 0.9, "2026-11-01", 365, TODAY)
        out = scoring.score("pricing", "outdated", 0.9, "2026-11-01", 365, TODAY)
        unk = scoring.score("pricing", "unclear", 0, None, 365, TODAY)
        self.assertEqual(scoring.label(sup), "current"); self.assertEqual(scoring.label(out), "changed")
        self.assertEqual(scoring.label(unk, "unclear"), "unverified"); self.assertTrue(out < unk < sup)
    def test_perishable_decays_faster(self):
        self.assertLess(scoring.prior("pricing", 365), scoring.prior("regulation", 365))
    def test_old_source_counts_less(self):
        self.assertGreater(scoring.score("pricing", "supported", 1, "2026-11-10", 0, TODAY), scoring.score("pricing", "supported", 1, "2024-01-01", 0, TODAY))
    def test_dates(self):
        self.assertEqual(scoring.parse_date("2026-10-02T10:00:00Z"), date(2026, 10, 2))
        self.assertIsNone(scoring.parse_date("garbage"))


class Claims(unittest.TestCase):
    def test_rules_find_perishable_facts_only(self):
        got = claims.heuristic_claims("The Pro plan costs $20 per month. The sky is blue. Dana Rao is the CEO of AcmeCloud.", TODAY)
        cats = {c["category"] for c in got}
        self.assertEqual(len(got), 2); self.assertEqual(cats, {"pricing", "leadership"})
    def test_falls_back_when_gateway_down(self):
        class Down:
            def chat_json(self, *a, **k): raise RuntimeError("down")
        got, method = claims.extract_claims("Version 2.1 is the latest release.", Down(), 8, TODAY)
        self.assertEqual(method, "rules"); self.assertTrue(got)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(); self.cfg.mock = True; self.deps = Deps(MockGateway(), MockSearch())
    def test_end_to_end_flags_changed_and_keeps_current(self):
        r = pipeline.verify(SAMPLE, self.deps, self.cfg, TODAY)
        by = {c["category"]: c for c in r["claims"]}
        self.assertEqual(by["pricing"]["label"], "changed"); self.assertEqual(by["version"]["label"], "changed")
        self.assertEqual(by["leadership"]["label"], "current")
        self.assertTrue(r["had_corrections"]); self.assertIn("$29", r["corrected_answer"])
        self.assertTrue(by["pricing"]["sources"][0]["url"].startswith("https://"))
        self.assertGreater(r["stats"]["bifrost"]["calls"], 0); self.assertGreater(r["stats"]["tavily"]["searches"], 0)
    def test_verdict_without_valid_source_is_discarded(self):
        class Liar(MockGateway):
            def chat_json(self, messages, max_tokens=900):
                if messages[0]["content"].startswith("TASK:JUDGE"):
                    return {"verdict": "outdated", "confidence": 1, "current_value": "x", "reason": "trust me", "evidence": [99]}
                return super().chat_json(messages, max_tokens)
        r = pipeline.verify("AcmeCloud's Pro plan costs $20 per month.", Deps(Liar(), MockSearch()), self.cfg, TODAY)
        self.assertEqual(r["claims"][0]["verdict"], "unclear"); self.assertFalse(r["had_corrections"])
    def test_search_failure_is_reported_not_hidden(self):
        class Broken(MockSearch):
            def search(self, *a, **k): raise RuntimeError("quota")
        r = pipeline.verify("AcmeCloud's Pro plan costs $20 per month.", Deps(MockGateway(), Broken()), self.cfg, TODAY)
        self.assertEqual(r["claims"][0]["label"], "unverified"); self.assertIn("quota", r["claims"][0]["reason"])


class GatewayAndSearch(unittest.TestCase):
    def test_bifrost_falls_back_to_next_model(self):
        cfg = Config(); cfg.models = ["a/primary", "b/backup"]; seen = []
        def fake(url, payload, headers=None, timeout=30):
            seen.append(payload["model"])
            if payload["model"] == "a/primary": raise fphttp.HttpError("provider down")
            return {"choices": [{"message": {"content": "{\"ok\": true}"}}], "model": "b/backup", "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
        orig, fphttp.post_json = fphttp.post_json, fake
        try:
            g = bifrost.Gateway(cfg); self.assertEqual(g.chat_json([{"role": "user", "content": "x"}]), {"ok": True})
        finally: fphttp.post_json = orig
        self.assertEqual(seen, ["a/primary", "b/backup"]); s = g.snapshot()
        self.assertEqual((s["fallbacks_used"], s["errors"], s["prompt_tokens"]), (1, 1, 5))
    def test_parse_json_handles_fences(self):
        self.assertEqual(bifrost.parse_json("```json\n{\"a\":1}\n```"), {"a": 1})
    def test_tavily_caches_and_sends_bearer(self):
        cfg = Config(); cfg.tavily_key = "tvly-test"; calls = []
        def fake(url, payload, headers=None, timeout=30):
            calls.append((payload, headers)); return {"results": [{"title": "T", "url": "https://x", "content": "c", "published_date": "2026-10-01"}]}
        orig, fphttp.post_json = fphttp.post_json, fake
        try:
            s = Search(cfg); a = s.search("q", time_range="year"); b = s.search("q", time_range="year")
        finally: fphttp.post_json = orig
        self.assertEqual(len(calls), 1); self.assertEqual(a, b); self.assertEqual(calls[0][1]["Authorization"], "Bearer tvly-test")
        self.assertEqual(s.snapshot()["cache_hits"], 1)
    def test_missing_key_is_clear(self):
        cfg = Config(); cfg.tavily_key = ""
        with self.assertRaises(RuntimeError): Search(cfg).search("q")


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = Config(); cfg.mock = True; App.cfg, App.deps = cfg, build(cfg)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), App); cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()
    def post(self, path, obj):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), json.dumps(obj).encode(), {"Content-Type": "application/json"})
        return urllib.request.urlopen(req)
    def test_verify_api_and_ui(self):
        r = json.loads(self.post("/api/verify", {"answer": SAMPLE}).read()); self.assertEqual(len(r["claims"]), 3)
        self.assertIn(b"FreshProxy", urllib.request.urlopen("http://127.0.0.1:%d/" % self.port).read())
        h = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/api/health" % self.port).read()); self.assertTrue(h["mock"])
    def test_empty_input_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as cm: self.post("/api/verify", {"answer": " "})
        self.assertEqual(cm.exception.code, 400)
    def test_openai_compatible_proxy_attaches_report(self):
        resp = self.post("/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        j = json.loads(resp.read()); self.assertIn("choices", j); self.assertIsNotNone(j["freshproxy"]["overall"])
        self.assertNotEqual(resp.headers["X-FreshProxy-Score"], "n/a")
    def test_streaming_refused_honestly(self):
        with self.assertRaises(urllib.error.HTTPError) as cm: self.post("/v1/chat/completions", {"stream": True, "messages": []})
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
