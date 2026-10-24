"""A tiny JSON-over-HTTPS client. Stdlib only.

`transport` is swappable so tests never touch the network. Errors never include request
headers or query strings, so tokens can't leak into logs, state files or reports.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]

USER_AGENT = "seo-engine/0.1 (+https://github.com/alvarezdaymion/seo-engine)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _message(data: Any) -> str:
    """The provider's error text: Google and GitHub use `message`, Shopify uses `errors`."""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message", ""))[:300]
    if isinstance(err, str):
        return err[:300]
    if data.get("message"):
        return str(data["message"])[:300]
    if data.get("errors"):
        return json.dumps(data["errors"])[:300]
    return ""


class Http:
    def __init__(self, headers: dict[str, str] | None = None, transport: Transport | None = None,
                 timeout: float = 60.0):
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def request(self, method: str, url: str, params: dict[str, Any] | None = None, json_body: Any = None,
                form: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> Any:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body, extra = None, dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode()
            extra["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            extra["Content-Type"] = "application/x-www-form-urlencoded"
        status, raw = self.transport(method, url, {**self.headers, **extra}, body, self.timeout)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if status >= 400:
            raise HttpError("%s %s returned HTTP %d %s" % (method, url.split("?")[0], status, _message(data)), status)
        return data

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", url, params=params)
