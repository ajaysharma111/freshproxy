import json, urllib.error, urllib.request


class HttpError(RuntimeError):
    pass


def post_json(url, payload, headers=None, timeout=30):
    """POST JSON, return parsed JSON. Tests replace this function (module attribute) with a fake."""
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "FreshProxy/0.1"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(10_000_000))
    except urllib.error.HTTPError as e:
        raise HttpError("HTTP %s from %s: %s" % (e.code, url, e.read(300).decode("utf-8", "replace"))) from None
    except Exception as e:  # noqa: BLE001
        raise HttpError("%s: %s" % (url, e)) from None
