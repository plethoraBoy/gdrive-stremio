"""Helpers shared by the metadata and stream modules."""

import json
import threading

import requests


_DEFAULT_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

_SESSION_LOCK = threading.Lock()
_SESSION = None


def _get_session():
    """Reuse one requests session (thread-safely) instead of pickling it."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = requests.Session()
            _SESSION.headers.update(_DEFAULT_HEADERS)
        return _SESSION


def hr_size(size):
    """Bytes to human readable; tolerates missing / invalid sizes."""
    try:
        size = float(size)
    except (TypeError, ValueError):
        size = 0.0
    if size < 0:
        size = 0.0
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if size < 1024.0:
            break
        size /= 1024.0
    return "{:.2f}{}".format(size, unit)


def safe_get(list_, idx, default=""):
    try:
        return list_[idx]
    except (IndexError, KeyError, TypeError):
        return default


def num_extract(string):
    num_chars = [ch if ch.isdigit() else " " for ch in str(string or "")]
    return "".join(num_chars).split()


def is_year(string):
    try:
        return 1850 <= int(string) <= 2050 and len(str(string)) == 4
    except (TypeError, ValueError):
        return False


def sanitize(string, valid_chars=". "):
    """Return alphanumeric input with certain non-alphanumeric chars intact."""
    chars = [
        ch if ch.isalnum() or ch in valid_chars else " "
        for ch in str(string or "")
    ]
    return " ".join("".join(chars).split()).lower()


def req_wrapper(url, time_out=5):
    """GET a URL and return its text, or '' on any network/HTTP error."""
    if not url:
        return ""
    url = str(url)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    session = _get_session()
    try:
        response = session.get(url, timeout=time_out)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        print("Request to {} failed: {}".format(url, exc))
        return ""


def req_api(url, key="meta"):
    """Fetch JSON from an API that sometimes wraps JSON in callbacks/noise.

    ``key=None`` returns the parsed document instead of one field.
    """
    text = req_wrapper(url)
    if not text:
        return None

    start = min(
        [pos for pos in (text.find("{"), text.find("[")) if pos >= 0] or [-1]
    )
    if start < 0:
        return None

    try:
        data = json.loads(text[start:].rstrip(") \n").strip())
    except (ValueError, TypeError):
        return None

    if key is None:
        return data
    if isinstance(data, dict):
        return data.get(key)
    if isinstance(data, list) and data:
        first = data[0]
        return first.get(key) if isinstance(first, dict) else None
    return None
