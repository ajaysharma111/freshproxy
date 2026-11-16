"""Freshness scoring.

Two signals are combined:
  prior   - how fast this *kind* of fact goes stale (half-life by category) and how old the AI answer is probably
  evidence - what live sources say right now (supported / contradicted / unclear) and how recent those sources are
The score is 0..1. >= 0.70 'current', <= 0.30 'changed' (only with contradicting evidence), otherwise 'unverified'.
"""
from datetime import date

HALF_LIFE_DAYS = {"pricing": 60, "version": 120, "leadership": 240, "product": 180, "regulation": 365,
                  "statistic": 365, "event": 730, "other": 1000}
CATEGORIES = tuple(HALF_LIFE_DAYS)


def prior(category, answer_age_days):
    hl = HALF_LIFE_DAYS.get(category, HALF_LIFE_DAYS["other"])
    return 0.5 ** (max(answer_age_days, 0) / hl)


def parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(str(s)).date()
    except (TypeError, ValueError):
        return None


def source_recency(category, published, today):
    d = parse_date(published)
    if not d:
        return 0.7                      # undated source: neutral-ish
    return 0.5 ** (max((today - d).days, 0) / HALF_LIFE_DAYS.get(category, 1000))


def score(category, verdict, confidence, newest_source_date, answer_age_days, today=None):
    today = today or date.today()
    conf = min(max(float(confidence or 0), 0.0), 1.0)
    if verdict == "supported":
        s = 0.55 + 0.45 * conf * source_recency(category, newest_source_date, today)
    elif verdict in ("contradicted", "outdated"):
        s = 0.02 + 0.13 * (1 - conf)
    else:
        s = 0.2 + 0.3 * prior(category, answer_age_days)   # no evidence: never as low as a confirmed change
    return round(min(max(s, 0.0), 1.0), 2)


def label(s, verdict=None):
    if verdict == "unclear":
        return "unverified"        # no evidence is never reported as 'changed'
    return "current" if s >= 0.70 else "changed" if s <= 0.30 else "unverified"


def overall(scores):
    if not scores:
        return None
    return round(0.5 * min(scores) + 0.5 * (sum(scores) / len(scores)), 2)
