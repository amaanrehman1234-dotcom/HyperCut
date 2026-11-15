"""Tiny urllib JSON client."""
import json
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get_json(url, data=None, headers=None, timeout=15):
    """GET url (POST data as JSON if given) and return the decoded JSON reply."""
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)
