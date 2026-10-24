"""A tiny JSON-over-HTTPS client. Stdlib only.

`transport` is swappable so tests never touch the network. Errors omit request data, provider
error text and underlying exceptions, which can all contain credentials.
"""

from __future__ import annotations

import json
import re
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


def credential(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or any(not 33 <= ord(c) <= 126 for c in value):
        raise RuntimeError("%s must be a nonempty ASCII token without whitespace or control characters" % name)
    return value


def _headers(headers: dict[str, str]) -> dict[str, str]:
    for name, value in headers.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or not isinstance(value, str) or value != value.strip()
                or any(not 32 <= ord(c) <= 126 for c in value)):
            raise HttpError("HTTP headers are invalid")
        if name.lower() in ("authorization", "proxy-authorization"):
            scheme, separator, token = value.partition(" ")
            if not separator:
                raise HttpError("HTTP authorization header is invalid")
            credential(scheme, "HTTP authorization scheme")
            credential(token, "HTTP authorization token")
        elif name.lower() in ("x-shopify-access-token", "x-api-key"):
            credential(value, "HTTP credential")
    return headers


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes]:
    try:
        req = urllib.request.Request(url, data=body, method=method, headers=_headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    except Exception:
        raise HttpError("HTTP request failed") from None


class Http:
    def __init__(self, headers: dict[str, str] | None = None, transport: Transport | None = None,
                 timeout: float = 60.0):
        try:
            self.headers = _headers({"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
        except Exception:
            raise HttpError("HTTP headers are invalid") from None
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def request(self, method: str, url: str, params: dict[str, Any] | None = None, json_body: Any = None,
                form: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> Any:
        try:
            if params:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port == 0
                    or parsed.username is not None or parsed.password is not None or parsed.fragment
                    or any(not 33 <= ord(c) <= 126 for c in url)):
                raise ValueError("invalid request URL")
            body, extra = None, dict(headers or {})
            if json_body is not None:
                body = json.dumps(json_body).encode()
                extra["Content-Type"] = "application/json"
            elif form is not None:
                body = urllib.parse.urlencode(form).encode()
                extra["Content-Type"] = "application/x-www-form-urlencoded"
            status, raw = self.transport(method, url, _headers({**self.headers, **extra}), body, self.timeout)
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError("invalid response status")
            try:
                data = json.loads(raw) if raw and status < 400 else None
            except ValueError:
                data = None
        except Exception:
            raise HttpError("HTTP request failed") from None
        if status >= 400:
            detail = "; authentication or permission denied" if status in (401, 403) else ""
            raise HttpError("HTTP request returned HTTP %d%s" % (status, detail), status)
        return data

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", url, params=params)
