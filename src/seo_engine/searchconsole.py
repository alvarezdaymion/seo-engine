"""Google Search Console Search Analytics API, read-only.

Env:
  GSC_SERVICE_ACCOUNT_FILE   path to a service-account JSON key. Add the service account's email
                             to the Search Console property as a user (Restricted is enough).
  GSC_ACCESS_TOKEN           alternative: an OAuth access token with the webmasters.readonly
                             scope. It expires after an hour, so it only suits a one-off run.

Options in seo-engine.toml ([search]):
  site_url   the property: "sc-domain:example.com" or "https://www.example.com/"
  country    optional three-letter country filter, for example "usa"

Reads POST /webmasters/v3/sites/<site>/searchAnalytics/query with dimensions date, query and
page and dataState "final", paginated with startRow. The token request asks only for the
webmasters.readonly scope. The service-account assertion is signed with the `openssl` command
so the runtime stays standard library only.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .http import Http, Transport
from .search import Row, SearchRows

API = "https://searchconsole.googleapis.com/webmasters/v3/sites/%s/searchAnalytics/query"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
MAX_PAGES = 40

Signer = Callable[[str, bytes], bytes]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def service_account_assertion(creds: dict[str, Any], now: int, signer: Signer) -> str:
    """A one-hour JWT asking for read-only Search Console access."""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iss": creds["client_email"], "scope": SCOPE, "aud": TOKEN_URI, "iat": now, "exp": now + 3600}
    signing_input = "%s.%s" % (_b64(json.dumps(header, separators=(",", ":")).encode()),
                               _b64(json.dumps(claims, separators=(",", ":")).encode()))
    return signing_input + "." + _b64(signer(creds["private_key"], signing_input.encode()))


def openssl_signer(private_key: str, data: bytes) -> bytes:
    """RS256 signature via `openssl dgst`. The key sits in a private temp dir for the call only."""
    if not shutil.which("openssl"):
        raise RuntimeError("the openssl command is needed to sign the Search Console token request")
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "key.pem"
        key.touch(mode=0o600)
        key.write_text(private_key)
        out = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=data,
                             capture_output=True, check=True, timeout=30)
    return out.stdout


class SearchConsoleSource:
    name = "searchconsole"

    def __init__(self, site_url: str, credentials_file: str | None = None, access_token: str | None = None,
                 country: str = "", transport: Transport | None = None, signer: Signer | None = None,
                 now: Callable[[], float] = time.time, row_limit: int = 25000):
        if not site_url:
            raise RuntimeError("set [search] site_url to your Search Console property")
        credentials_file = credentials_file or os.environ.get("GSC_SERVICE_ACCOUNT_FILE", "")
        self._static_token = access_token or os.environ.get("GSC_ACCESS_TOKEN", "")
        self.creds: dict[str, Any] = {}
        if not self._static_token:
            if not credentials_file:
                raise RuntimeError("set GSC_SERVICE_ACCOUNT_FILE (or GSC_ACCESS_TOKEN)")
            self.creds = json.loads(Path(credentials_file).expanduser().read_text())
            if self.creds.get("token_uri", TOKEN_URI) != TOKEN_URI:
                raise RuntimeError("unexpected token_uri in the service-account key")
        self.site_url = site_url
        self.country = country
        self.http = Http(transport=transport)
        self.signer = signer or openssl_signer
        self.now = now
        self.row_limit = row_limit
        self._token, self._expires = "", 0

    def _access_token(self) -> str:
        if self._static_token:
            return self._static_token
        now = int(self.now())
        if self._token and now < self._expires - 60:
            return self._token
        assertion = service_account_assertion(self.creds, now, self.signer)
        resp = self.http.request("POST", TOKEN_URI, form={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        if not (resp or {}).get("access_token"):
            raise RuntimeError("Google returned no access token")
        self._token, self._expires = resp["access_token"], now + int(resp.get("expires_in", 3600))
        return self._token

    def fetch(self, start: dt.date, end: dt.date) -> list[Row]:
        url = API % urllib.parse.quote(self.site_url, safe="")
        payload: dict[str, Any] = {
            "startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date", "query", "page"],
            "type": "web", "dataState": "final", "rowLimit": self.row_limit, "startRow": 0,
        }
        if self.country:
            payload["dimensionFilterGroups"] = [{"filters": [
                {"dimension": "country", "operator": "equals", "expression": self.country}]}]
        rows: list[Row] = []
        for _ in range(MAX_PAGES):
            body = self.http.request("POST", url, json_body=payload,
                                     headers={"Authorization": "Bearer " + self._access_token()}) or {}
            page = body.get("rows", [])
            for r in page:
                keys = r.get("keys") or []
                if len(keys) == 3:
                    rows.append(Row(keys[0], keys[1], keys[2], float(r.get("clicks") or 0),
                                    float(r.get("impressions") or 0), float(r.get("position") or 0)))
            if len(page) < self.row_limit:
                break
            payload["startRow"] += len(page)
        else:
            raise RuntimeError("Search Console pagination limit reached; data is unavailable")
        return SearchRows(rows)
