import base64
import datetime as dt
import json
import urllib.parse

import pytest

from seo_engine.http import HttpError
from seo_engine.searchconsole import SCOPE, TOKEN_URI, SearchConsoleSource, openssl_signer, service_account_assertion

CREDS = {"client_email": "reader@example.com", "private_key": "not-a-real-key",
         "token_uri": TOKEN_URI}
ACCESS = "ya29-test-token-not-real"


@pytest.fixture(autouse=True)
def no_google_env(monkeypatch):
    monkeypatch.delenv("GSC_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("GSC_ACCESS_TOKEN", raising=False)


def decode(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class FakeGoogle:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        if url == TOKEN_URI:
            return 200, json.dumps({"access_token": ACCESS, "expires_in": 3600}).encode()
        assert headers["Authorization"] == "Bearer " + ACCESS
        return 200, json.dumps({"rows": self.pages.pop(0) if self.pages else []}).encode()


def row(day, query):
    return {"keys": [day, query, "https://www.example.com/"], "clicks": 1, "impressions": 10, "position": 4.5}


def make(fake, tmp_path, **kw):
    key = tmp_path / "key.json"
    key.write_text(json.dumps(CREDS))
    return SearchConsoleSource("sc-domain:example.com", credentials_file=str(key), transport=fake,
                               signer=lambda pem, data: b"signature", now=lambda: 1_800_000_000, **kw)


def test_the_assertion_asks_for_read_only_access():
    jwt = service_account_assertion(CREDS, 1_800_000_000, lambda pem, data: b"sig")
    header, claims, sig = jwt.split(".")
    assert decode(header) == {"alg": "RS256", "typ": "JWT"}
    assert decode(claims)["scope"] == SCOPE and SCOPE.endswith("webmasters.readonly")
    assert decode(claims)["exp"] - decode(claims)["iat"] == 3600


def test_fetch_reads_final_daily_query_page_rows_and_pages(tmp_path):
    fake = FakeGoogle([[row("2026-03-01", "a"), row("2026-03-01", "b")], [row("2026-03-02", "c")]])
    src = make(fake, tmp_path, row_limit=2, country="usa")
    rows = src.fetch(dt.date(2026, 2, 1), dt.date(2026, 3, 2))
    assert [(r.date, r.query, r.position) for r in rows] == [("2026-03-01", "a", 4.5), ("2026-03-01", "b", 4.5),
                                                              ("2026-03-02", "c", 4.5)]
    token_call, *api_calls = fake.calls
    grant = urllib.parse.parse_qs(token_call[3].decode())["grant_type"]
    assert grant == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    first = json.loads(api_calls[0][3])
    assert first["dimensions"] == ["date", "query", "page"] and first["dataState"] == "final"
    assert first["dimensionFilterGroups"][0]["filters"][0]["expression"] == "usa"
    assert [json.loads(c[3])["startRow"] for c in api_calls] == [0, 2]
    assert "sc-domain%3Aexample.com" in api_calls[0][1]
    assert len([c for c in fake.calls if c[1] == TOKEN_URI]) == 1  # the token is reused


def test_an_access_token_skips_the_exchange(monkeypatch):
    monkeypatch.delenv("GSC_SERVICE_ACCOUNT_FILE", raising=False)
    fake = FakeGoogle([[row("2026-03-01", "a")]])
    src = SearchConsoleSource("sc-domain:example.com", access_token=ACCESS, transport=fake)
    assert len(src.fetch(dt.date(2026, 3, 1), dt.date(2026, 3, 1))) == 1
    assert all(c[1] != TOKEN_URI for c in fake.calls)


def test_pagination_limit_is_unavailable_data(tmp_path, monkeypatch):
    monkeypatch.setattr("seo_engine.searchconsole.MAX_PAGES", 1)
    fake = FakeGoogle([[row("2026-03-01", "synthetic question")]])
    with pytest.raises(RuntimeError, match="pagination"):
        make(fake, tmp_path, row_limit=1).fetch(dt.date(2026, 3, 1), dt.date(2026, 3, 2))


def test_errors_never_include_the_token(tmp_path):
    def failing(method, url, headers, body, timeout):
        if url == TOKEN_URI:
            return 200, json.dumps({"access_token": ACCESS}).encode()
        return 403, b'{"error": {"message": "User does not have sufficient permission"}}'
    with pytest.raises(HttpError) as e:
        make(failing, tmp_path).fetch(dt.date(2026, 3, 1), dt.date(2026, 3, 2))
    assert "403" in str(e.value) and ACCESS not in str(e.value) and "permission" in str(e.value)


def test_credentials_and_token_uri_are_checked(tmp_path, monkeypatch):
    monkeypatch.delenv("GSC_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("GSC_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GSC_SERVICE_ACCOUNT_FILE"):
        SearchConsoleSource("sc-domain:example.com")
    key = tmp_path / "key.json"
    key.write_text(json.dumps({**CREDS, "token_uri": "https://token.example.net/"}))
    with pytest.raises(RuntimeError, match="token_uri"):
        SearchConsoleSource("sc-domain:example.com", credentials_file=str(key))


def test_signing_errors_never_expose_the_private_key():
    def signer(key, data):
        raise ValueError("could not sign with " + key)

    with pytest.raises(RuntimeError) as caught:
        service_account_assertion(CREDS, 1_800_000_000, signer)
    assert CREDS["private_key"] not in str(caught.value)


@pytest.mark.parametrize("token", [ACCESS + "\n", " " + ACCESS, ACCESS + "\t", ACCESS + "é", {"token": ACCESS}])
def test_issued_tokens_are_validated_before_an_api_request(tmp_path, token):
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append(url)
        return 200, json.dumps({"access_token": token}).encode()

    with pytest.raises(RuntimeError) as caught:
        make(transport, tmp_path).fetch(dt.date(2026, 3, 1), dt.date(2026, 3, 2))
    assert calls == [TOKEN_URI] and ACCESS not in str(caught.value)


def test_invalid_token_expiry_never_exposes_the_response(tmp_path):
    def transport(*args):
        return 200, json.dumps({"access_token": ACCESS, "expires_in": ACCESS}).encode()

    with pytest.raises(RuntimeError) as caught:
        make(transport, tmp_path).fetch(dt.date(2026, 3, 1), dt.date(2026, 3, 2))
    assert ACCESS not in str(caught.value)


@pytest.mark.parametrize("key", ["", " ", "synthetic-key\x00", "synthetic-keyé", {"key": "synthetic-key"}])
def test_invalid_private_keys_are_rejected_before_signing(monkeypatch, key):
    calls = []
    monkeypatch.setattr("seo_engine.searchconsole.shutil.which", lambda name: "openssl")

    def temporary():
        calls.append(1)
        raise AssertionError("must not create a signing file")

    monkeypatch.setattr("seo_engine.searchconsole.tempfile.TemporaryDirectory", temporary)
    with pytest.raises(RuntimeError) as caught:
        openssl_signer(key, b"synthetic assertion")
    assert calls == [] and "synthetic-key" not in str(caught.value)
