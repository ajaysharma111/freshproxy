"""Measure how well FreshProxy separates stale from current claims.

  python benchmark/run.py            # live: needs TAVILY_API_KEY and a Bifrost gateway
  python benchmark/run.py --limit 4  # cheaper smoke run
An answer is predicted 'stale' when any of its claims is labelled 'changed'. 'unverified' counts as not-stale, so
a missing key shows up as low recall instead of fake precision."""
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from freshproxy.config import Config  # noqa: E402
from freshproxy.server import build  # noqa: E402
from freshproxy.pipeline import verify  # noqa: E402


def run(items, deps, cfg):
    tp = fp = tn = fn = 0
    rows = []
    for it in items:
        rep = verify(it["answer"], deps, cfg)
        pred = "stale" if any(c["label"] == "changed" for c in rep["claims"]) else "current"
        ok = pred == it["expected"]
        tp += pred == "stale" and it["expected"] == "stale"
        fp += pred == "stale" and it["expected"] == "current"
        tn += pred == "current" and it["expected"] == "current"
        fn += pred == "current" and it["expected"] == "stale"
        rows.append((it["id"], it["expected"], pred, rep["overall"], ok))
    n = max(len(items), 1)
    return {"accuracy": round((tp + tn) / n, 2), "precision": round(tp / (tp + fp), 2) if tp + fp else None,
            "recall": round(tp / (tp + fn), 2) if tp + fn else None, "tp": tp, "fp": fp, "tn": tn, "fn": fn, "rows": rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dataset", default=str(Path(__file__).with_name("dataset.json")))
    a = ap.parse_args()
    cfg = Config()
    items = json.loads(Path(a.dataset).read_text())["items"]
    if a.limit:
        items = items[:a.limit]
    res = run(items, build(cfg), cfg)
    for r in res["rows"]:
        print("%-16s expected=%-8s predicted=%-8s freshness=%-5s %s" % (r[0], r[1], r[2], r[3], "ok" if r[4] else "MISS"))
    print({k: v for k, v in res.items() if k != "rows"})
    if cfg.mock:
        print("NOTE: mock mode uses fictional data; these numbers mean nothing. Run with real keys.")
