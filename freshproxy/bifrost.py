"""All model calls go through Bifrost (Maxim AI's open-source LLM gateway, OpenAI-compatible API).

FreshProxy never talks to a model provider directly, so provider choice, keys, failover, caching, budgets and
observability live in the gateway. As a second safety net, we also retry down our own model list.
"""
import json, re, threading, time
from . import http


class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stats = {"calls": 0, "errors": 0, "fallbacks_used": 0, "prompt_tokens": 0, "completion_tokens": 0, "models_used": {}}
        self._lock = threading.Lock()

    def _bump(self, **kw):
        with self._lock:
            for k, v in kw.items():
                self.stats[k] += v

    def chat(self, messages, json_mode=False, max_tokens=900):
        headers = {"Authorization": "Bearer " + self.cfg.bifrost_key} if self.cfg.bifrost_key else {}
        last = None
        for i, model in enumerate(self.cfg.models):
            body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            self._bump(calls=1)
            try:
                r = http.post_json(self.cfg.bifrost_url + "/v1/chat/completions", body, headers, timeout=60)
                text = r["choices"][0]["message"]["content"] or ""
                u = r.get("usage") or {}
                with self._lock:
                    self.stats["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                    self.stats["completion_tokens"] += int(u.get("completion_tokens") or 0)
                    used = r.get("model") or model
                    self.stats["models_used"][used] = self.stats["models_used"].get(used, 0) + 1
                    if i:
                        self.stats["fallbacks_used"] += 1
                return text
            except Exception as e:  # noqa: BLE001
                last = e
                self._bump(errors=1)
        raise RuntimeError("Bifrost request failed for every configured model: %s" % last)

    def chat_json(self, messages, max_tokens=900):
        return parse_json(self.chat(messages, json_mode=True, max_tokens=max_tokens))

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.stats))


def parse_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))
