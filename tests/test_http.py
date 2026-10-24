import json
import traceback

import pytest

from seo_engine.http import Http, HttpError

SECRET = "synthetic-credential-marker"
URL = "https://api.example.com/" + SECRET


@pytest.mark.parametrize("error", [ValueError, OSError, RuntimeError, HttpError])
@pytest.mark.parametrize("encoding", ["json_body", "form"])
def test_request_exceptions_never_expose_request_credentials(error, encoding):
    calls = []

    def fail(method, url, headers, body, timeout):
        calls.append(method)
        raise error("request failed: %s %r %r" % (url, headers, body))

    client = Http({"Authorization": "Bearer " + SECRET}, transport=fail)
    with pytest.raises(HttpError) as caught:
        client.request("POST", URL, **{encoding: {"assertion": SECRET}})
    assert caught.value.status == 0 and calls == ["POST"]
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("body", [
    {"message": SECRET}, {"error": {"message": SECRET}}, {"errors": {"token": SECRET}},
])
def test_http_failures_omit_urls_and_provider_error_text(body):
    client = Http(transport=lambda *args: (403, json.dumps(body).encode()))
    with pytest.raises(HttpError) as caught:
        client.get(URL, {"access_token": SECRET})
    assert caught.value.status == 403 and "403" in str(caught.value)
    assert SECRET not in str(caught.value)


def test_request_preparation_errors_are_sanitized_before_transport():
    calls = []

    class BadValue:
        def __str__(self):
            raise ValueError(SECRET)

    client = Http(transport=lambda *args: calls.append(args))
    with pytest.raises(HttpError) as caught:
        client.get(URL, {"assertion": BadValue()})
    assert calls == [] and SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("value", [SECRET + "\n", SECRET + "\r", SECRET + "\x00", SECRET + "\t", SECRET + "é"])
@pytest.mark.parametrize("per_request", [False, True])
def test_invalid_headers_are_refused_before_transport(value, per_request):
    calls = []
    headers = {"Authorization": "Bearer " + value}
    with pytest.raises(HttpError) as caught:
        client = Http({} if per_request else headers, transport=lambda *args: calls.append(args))
        client.request("POST", URL, headers=headers if per_request else None)
    assert calls == [] and SECRET not in str(caught.value)


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer " + SECRET + " extra"}, {"Authorization": "Bearer  " + SECRET},
    {"Authorization": SECRET}, {"X-Shopify-Access-Token": ""}, {"X-Api-Key": SECRET + " extra"},
])
def test_credential_headers_require_nonempty_tokens_without_whitespace(headers):
    calls = []
    with pytest.raises(HttpError) as caught:
        Http(headers, transport=lambda *args: calls.append(args)).get(URL)
    assert calls == [] and SECRET not in str(caught.value)
