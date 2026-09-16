"""Construction, signing, error mapping, retries, and transport faults."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from huuray import (
    AsyncHuurayClient,
    HuurayAPIError,
    HuurayAuthError,
    HuurayClient,
    HuurayConfigError,
    HuurayConnectionError,
    HuurayIndeterminateOrderError,
    HuurayNotFoundError,
    HuurayServerError,
    HuurayTimeoutError,
    HuurayValidationError,
    RetryOptions,
)
from huuray.auth import sign_request
from huuray.resources._base import Operation

from .helpers import (
    MockResponse,
    RecordingTransport,
    make_async_client,
    make_client,
)


def assert_not_quoted(error: BaseException, marker: str) -> None:
    """``marker`` is nowhere in the error, and nothing is chained that could hold it."""
    assert marker not in str(error)
    assert marker not in repr(error)
    assert marker not in repr(error.args)
    assert error.__cause__ is None
    assert error.__context__ is None


class TestConstruction:
    def test_requires_an_api_token(self):
        with pytest.raises(HuurayConfigError):
            HuurayClient(api_token="", api_secret="s")

    def test_requires_an_api_secret(self):
        with pytest.raises(HuurayConfigError):
            HuurayClient(api_token="t", api_secret="")

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "bad",
        [
            # os.environ decodes an undecodable byte on POSIX to a lone surrogate.
            # Signing then raised a raw UnicodeEncodeError whose repr and args
            # carried the secret and the nonce.
            "leaky-secret\udcff",
            "\ud800leaky-secret",
            "leaky\udfffsecret",
        ],
    )
    def test_rejects_an_api_secret_that_cannot_be_utf8_encoded_without_quoting_it(
        self, client_class, bad
    ):
        with pytest.raises(HuurayConfigError, match="unpaired surrogate") as caught:
            client_class(api_token="t", api_secret=bad)
        assert_not_quoted(caught.value, "leaky")

    def test_accepts_a_non_ascii_api_secret_and_signs_it_as_utf8(self):
        secret = "sëcret-\U0001f511"
        transport = RecordingTransport()
        client = HuurayClient(
            api_token="t",
            api_secret=secret,
            nonce_factory=lambda: "n",
            transport=httpx.MockTransport(transport.handle_request),
        )
        client.balances.list()
        assert transport.calls[0].headers["x-api-hash"] == sign_request(secret, "n")

    @pytest.mark.parametrize(
        "bad",
        [
            # "/v4" is the case that differs by platform in other languages:
            # not absolute on Windows, a valid file:// URI on Linux and macOS.
            # Validating the scheme makes the behaviour identical everywhere.
            "/v4",
            "v4",
            "api.huuray.com",
            "file:///etc/passwd",
            "ftp://example.test",
        ],
    )
    def test_rejects_a_base_url_that_is_not_absolute_http(self, bad):
        with pytest.raises(HuurayConfigError):
            HuurayClient(api_token="t", api_secret="s", base_url=bad)

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "bad", ["ftp://leakyuser:leakypw@example.test", "leaky.example.test/v4", "file:///leaky"]
    )
    def test_the_not_absolute_http_error_does_not_quote_the_base_url(self, client_class, bad):
        # It used to: a password in the URL was repeated in the message.
        with pytest.raises(HuurayConfigError, match=r"not an absolute http\(s\) URL") as caught:
            client_class(api_token="t", api_secret="s", base_url=bad)
        assert "https://api.huuray.com" in str(caught.value)
        assert_not_quoted(caught.value, "leaky")

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "bad",
        [
            # httpx sent user-info to the host as Basic credentials on every request.
            "https://leakyuser:leakypw@example.test",
            "https://leakyuser@example.test/",
            "https://:leakypw@example.test",
            "http://leakyuser:leakypw@127.0.0.1:8080/v4-proxy/",
            "https://@example.test",
        ],
    )
    def test_rejects_a_base_url_with_user_info_without_quoting_it(self, client_class, bad):
        with pytest.raises(HuurayConfigError, match="user-info") as caught:
            client_class(api_token="t", api_secret="s", base_url=bad)
        assert "https://api.huuray.com" in str(caught.value)
        assert_not_quoted(caught.value, "leaky")

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "bad",
        [
            # The path was appended after it, so every request went to the wrong path.
            "https://example.test/?leaky=1",
            "https://example.test?leaky",
            "https://example.test/v4?",
            "https://example.test/#leaky",
            "https://example.test#",
            "https://leakyuser:leakypw@example.test/?x",
        ],
    )
    def test_rejects_a_base_url_with_a_query_or_fragment_without_quoting_it(
        self, client_class, bad
    ):
        with pytest.raises(HuurayConfigError, match=r"query \(\?\) or fragment \(#\)") as caught:
            client_class(api_token="t", api_secret="s", base_url=bad)
        assert "https://api.huuray.com" in str(caught.value)
        assert_not_quoted(caught.value, "leaky")

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "bad",
        [
            # Accepted here, then a raw httpx.InvalidURL at the first request.
            "http://127.0.0.1:abc",
            "http://127.0.0.1:5%",
            # httpx does not check the range, or that there is a host.
            "http://127.0.0.1:99999",
            "http://127.0.0.1:0",
            "http://leaky.example.test:65536",
            "http://:80",
            # A raw idna.IDNAError at the first request.
            "http://xn--",
            # A raw UnicodeError from the sync transport at the first request.
            "http://a..b",
            "https://.",
            "https://" + "a" * 64 + ".example.test",
            "https://127.0.0.1:99999",
            # A raw ValueError from urlsplit() here, some quoting the host.
            "http://[::1",
            "http://[leaky]",
        ],
    )
    def test_rejects_a_base_url_whose_host_or_port_cannot_be_used_without_quoting_it(
        self, client_class, bad
    ):
        with pytest.raises(HuurayConfigError, match="empty or invalid host") as caught:
            client_class(api_token="t", api_secret="s", base_url=bad)
        assert "https://api.huuray.com" in str(caught.value)
        assert_not_quoted(caught.value, bad)
        assert_not_quoted(caught.value, "leaky")

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "good",
        [
            "https://api.huuray.com",
            "http://localhost:8080",
            "http://127.0.0.1:1",
            "http://127.0.0.1:65535",
            "http://[::1]:8080",
            "https://xn--bcher-kva.example",
        ],
    )
    def test_accepts_an_absolute_http_base_url(self, client_class, good):
        # A fake transport only keeps each case from building a TLS context.
        transport = httpx.MockTransport(lambda _: httpx.Response(200))
        client = client_class(api_token="t", api_secret="s", base_url=good, transport=transport)
        assert client._base_url == good

    @pytest.mark.parametrize(
        "bad",
        [
            "https://api.huuray.com/ leaky",
            "https://api leaky.huuray.com",
            "https://api.huuray.com\r\nleaky",
            "https://api.huuray.com\tleaky",
            "https://api.huuray.com/leaky\x00",
            "https://äpi.leaky.example",
            "https://api.huuray.com/ä/leaky",
        ],
    )
    def test_rejects_a_base_url_with_a_space_control_or_non_ascii_character(self, bad):
        # urlsplit() accepts every one of these. httpx then refused them, or
        # percent-encoded them onto every request path, only at request time.
        with pytest.raises(
            HuurayConfigError, match="space, control character or non-ASCII"
        ) as caught:
            HuurayClient(api_token="t", api_secret="s", base_url=bad)
        assert "leaky" not in str(caught.value)

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "bad",
        [
            "leaky-token\r\nX-Injected: yes",
            "leaky-token\n",
            "leaky-token\x00",
            "leaky\ttoken",
            "leaky-token\x7f",
            "leaky-token\x01",
            "leaky-tökén",
            "leaky-token ",
            " leaky-token",
        ],
    )
    def test_rejects_an_api_token_that_cannot_be_sent_as_a_header_without_quoting_it(
        self, client_class, bad
    ):
        # Left to httpx, a line break or a stray space failed only at send time, as
        # a connection error quoting the token — for an order, as an indeterminate
        # one. A tab, DEL or other control character reached the wire; a non-ASCII
        # character raised a raw UnicodeEncodeError whose repr carries the token.
        with pytest.raises(HuurayConfigError, match="X-API-TOKEN") as caught:
            client_class(api_token=bad, api_secret="s")
        assert "leaky" not in str(caught.value)
        assert "leaky" not in repr(caught.value)

    @pytest.mark.parametrize("blank", ["   ", "\t", "\r\n"])
    def test_a_whitespace_only_api_token_counts_as_missing(self, blank):
        with pytest.raises(HuurayConfigError, match="api_token is required"):
            HuurayClient(api_token=blank, api_secret="s")

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "bad",
        [
            "payroll/2.1\r\nX-Injected: yes",
            "payroll/2.1\x00",
            "payroll\t2.1",
            "payroll/2.1\x7f",
            "payroll/2.1\x1b[31m",
            "payroll/2.1 ",
            "pæyroll/2.1",
            "   ",
        ],
    )
    def test_rejects_a_user_agent_that_cannot_be_sent_as_a_header_without_quoting_it(
        self, client_class, bad
    ):
        with pytest.raises(HuurayConfigError, match="User-Agent") as caught:
            client_class(api_token="t", api_secret="s", user_agent=bad)
        assert "yroll" not in str(caught.value)

    @pytest.mark.parametrize("client_class", [HuurayClient, AsyncHuurayClient])
    @pytest.mark.parametrize(
        "bad",
        [
            # 0 made an order that never left raise HuurayIndeterminateOrderError; a
            # negative value did too on the async client, and raised a raw ValueError
            # on the sync one.
            0,
            0.0,
            -1,
            -0.001,
            float("-inf"),
            # No timeout at all on the async client (and None on both), so a hung
            # order never raised; a raw ValueError or OverflowError on the sync one.
            float("nan"),
            float("inf"),
            None,
            1e300,
            # One millisecond past INT_MAX ms: OverflowError on Windows, and a C int
            # cast for poll() that can wrap on Linux and macOS.
            2147483.648,
            True,
            "30",
        ],
    )
    def test_rejects_a_timeout_the_runtime_would_not_honour(self, client_class, bad):
        with pytest.raises(HuurayConfigError, match=r"greater than 0 and at most 2147483\.647"):
            client_class(api_token="t", api_secret="s", timeout=bad)

    @pytest.mark.parametrize("good", [0.001, 1, 30.0, 2147483.647])
    def test_accepts_a_timeout_in_range_and_attaches_it_to_every_request(self, good):
        client, _ = make_client(timeout=good)
        request = client._build_request(Operation(method="GET", path="/v4/Balance"))
        assert request.extensions["timeout"] == {
            "connect": good,
            "read": good,
            "write": good,
            "pool": good,
        }

    def test_defaults_to_the_production_host(self):
        client, calls = make_client()
        client.balances.list()
        # Pins the actual origin, not just the path — a typo in
        # DEFAULT_BASE_URL must not ship green.
        assert calls[0].origin == "https://api.huuray.com"
        assert calls[0].path == "/v4/Balance"

    def test_accepts_a_base_url_with_a_trailing_slash(self):
        client, calls = make_client(base_url="https://example.test/")
        client.balances.list()
        assert calls[0].origin == "https://example.test"
        assert calls[0].path == "/v4/Balance"

    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("https://example.test/", "https://example.test/v4/Balance"),
            ("http://[::1]:8080/", "http://[::1]:8080/v4/Balance"),
            ("https://example.test/v4-proxy/", "https://example.test/v4-proxy/v4/Balance"),
        ],
    )
    async def test_the_async_client_accepts_a_base_url_with_a_trailing_slash(
        self, base_url, expected
    ):
        client, calls = make_async_client(base_url=base_url)
        async with client:
            await client.balances.list()
        assert calls[0].url == expected

    def test_works_as_a_context_manager(self):
        client, calls = make_client()
        with client as entered:
            entered.balances.list()
        assert len(calls) == 1


class TestSigningPerRequest:
    def test_sends_the_three_auth_headers_on_every_call(self):
        client, calls = make_client()
        client.balances.list()
        client.templates.list()
        assert len(calls) == 2
        for call in calls:
            assert call.headers["x-api-token"] == "test-token"
            assert call.headers["x-api-nonce"]
            assert re.fullmatch(r"[0-9a-f]{128}", call.headers["x-api-hash"])

    def test_uses_a_fresh_nonce_for_every_request(self):
        client, calls = make_client()
        client.balances.list()
        client.balances.list()
        client.balances.list()
        assert len({call.headers["x-api-nonce"] for call in calls}) == 3

    def test_never_sends_the_secret(self):
        client, calls = make_client()
        client.balances.list()
        assert "test-secret" not in json.dumps(calls[0].headers)

    def test_honours_a_hash_encoding_override(self):
        client, calls = make_client(hash_encoding="base64")
        client.balances.list()
        assert not re.fullmatch(r"[0-9a-f]{128}", calls[0].headers["x-api-hash"])

    def test_identifies_itself_and_appends_a_caller_supplied_agent(self):
        client, calls = make_client(user_agent="payroll/2.1")
        client.balances.list()
        assert calls[0].headers["user-agent"].startswith("huuray-python/")
        assert calls[0].headers["user-agent"].endswith("payroll/2.1")

    def test_a_custom_nonce_factory_is_used_for_signing(self):
        client, calls = make_client(nonce_factory=lambda: "fixed-nonce")
        client.balances.list()
        assert calls[0].headers["x-api-nonce"] == "fixed-nonce"

    def test_a_custom_nonce_factory_over_the_limit_is_rejected_before_sending(self):
        client, calls = make_client(nonce_factory=lambda: "x" * 51)
        with pytest.raises(ValueError, match="at most 50"):
            client.balances.list()
        assert calls == []

    @pytest.mark.parametrize(
        "bad",
        [
            # Sent as an empty X-API-NONCE, which the specification marks required.
            "",
            " ",
            "leaky-nonce\r\nX-Injected: yes",
            "leaky-nonce\x00",
            "leaky\tnonce",
            "leaky-nonce\x7f",
            "leaky-nonce\x01",
            "leaky-nöncé",
            "leaky-nonce ",
        ],
    )
    def test_a_custom_nonce_that_cannot_be_sent_is_rejected_before_sending_without_quoting_it(
        self, bad
    ):
        client, calls = make_client(nonce_factory=lambda: bad)
        with pytest.raises(ValueError, match="X-API-NONCE") as caught:
            client.balances.list()
        assert calls == []
        assert "leaky" not in str(caught.value)
        assert "leaky" not in repr(caught.value)
        assert caught.value.__cause__ is None

    def test_an_unsendable_nonce_on_an_order_is_a_value_error_not_an_indeterminate_order(self):
        client, calls = make_client(nonce_factory=lambda: "nonce\r\nX-Injected: yes")
        with pytest.raises(ValueError, match="X-API-NONCE"):
            client.orders.create(
                product_token="t", value=100, currency="DKK", quantity=1, ref_id="r"
            )
        assert calls == []


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, HuurayAuthError),
            (403, HuurayAuthError),
            (404, HuurayNotFoundError),
            (422, HuurayValidationError),
            (500, HuurayServerError),
            (400, HuurayAPIError),
        ],
    )
    def test_maps_each_http_status_to_the_right_error_type(self, status, expected):
        client, _ = make_client(
            MockResponse(status=status, json={"Status": status, "StatusMessage": "nope"})
        )
        with pytest.raises(expected):
            client.balances.list()

    def test_prefers_status_message_over_the_deprecated_message_field(self):
        client, _ = make_client(
            MockResponse(
                status=400,
                json={"Status": 400, "Message": "old text", "StatusMessage": "new text"},
            )
        )
        with pytest.raises(HuurayAPIError) as caught:
            client.balances.list()
        assert caught.value.status_message == "new text"

    def test_falls_back_to_message_when_status_message_is_absent(self):
        client, _ = make_client(
            MockResponse(status=400, json={"Status": 400, "Message": "old text"})
        )
        with pytest.raises(HuurayAPIError) as caught:
            client.balances.list()
        assert caught.value.status_message == "old text"

    def test_exposes_the_http_status_and_the_parsed_body(self):
        client, _ = make_client(
            MockResponse(status=422, json={"Status": 422, "StatusMessage": "bad"})
        )
        with pytest.raises(HuurayValidationError) as caught:
            client.balances.list()
        assert caught.value.http_status == 422
        assert caught.value.status == 422
        assert caught.value.method == "GET"
        assert caught.value.path == "/v4/Balance"

    def test_redacts_bearer_and_contact_fields_from_the_retained_error_body(self):
        client, _ = make_client(
            MockResponse(
                status=400,
                json={
                    "Status": 400,
                    "StatusMessage": "bad",
                    "Code": "LEAKED-CODE",
                    "Email": "jane@example.com",
                },
            )
        )
        with pytest.raises(HuurayAPIError) as caught:
            client.balances.list()
        dumped = json.dumps(caught.value.body)
        assert "LEAKED-CODE" not in dumped
        assert "jane@example.com" not in dumped

    def test_survives_a_non_json_error_body(self):
        client, _ = make_client(MockResponse(status=502, text="<html>bad gateway</html>"))
        with pytest.raises(HuurayServerError) as caught:
            client.balances.list()
        assert caught.value.status_message is None


class TestRetryPolicy:
    def test_retries_a_read_on_503(self):
        client, calls = make_client(
            [MockResponse(status=503), MockResponse(json={"Balances": []})],
            retry=RetryOptions(max_retries=2, base_delay=0.001),
        )
        client.balances.list()
        assert len(calls) == 2

    def test_never_retries_an_order_even_on_503(self):
        client, calls = make_client(
            MockResponse(status=503), retry=RetryOptions(max_retries=3, base_delay=0.001)
        )
        with pytest.raises(HuurayIndeterminateOrderError):
            client.orders.create(product_token="t", value=100, currency="DKK", quantity=1)
        assert len(calls) == 1

    def test_never_retries_a_resend_it_would_re_deliver_real_value(self):
        client, calls = make_client(
            MockResponse(status=503), retry=RetryOptions(max_retries=3, base_delay=0.001)
        )
        with pytest.raises(HuurayServerError):
            client.orders.resend(order_uid="x")
        assert len(calls) == 1

    def test_never_retries_a_cancel(self):
        client, calls = make_client(
            MockResponse(status=503), retry=RetryOptions(max_retries=3, base_delay=0.001)
        )
        with pytest.raises(HuurayServerError):
            client.orders.cancel(order_uid="x")
        assert len(calls) == 1

    def test_does_not_retry_a_400_the_request_is_wrong_repeating_will_not_help(self):
        client, calls = make_client(
            MockResponse(status=400), retry=RetryOptions(max_retries=3, base_delay=0.001)
        )
        with pytest.raises(HuurayAPIError):
            client.balances.list()
        assert len(calls) == 1

    def test_retries_a_read_after_a_connection_failure(self):
        client, calls = make_client(
            [
                MockResponse(raises=httpx.ConnectError("refused")),
                MockResponse(json={"Balances": []}),
            ],
            retry=RetryOptions(max_retries=2, base_delay=0.001),
        )
        assert client.balances.list().balances == []
        assert len(calls) == 2

    def test_clamps_a_negative_max_retries_to_zero_instead_of_never_sending(self):
        client, calls = make_client(retry=RetryOptions(max_retries=-3))
        client.balances.list()
        assert len(calls) == 1

    def test_the_default_policy_retries_reads(self, monkeypatch):
        # Constructed with NO `retry` argument at all, bypassing make_client's
        # setdefault: this is the only test that exercises the DEFAULT_RETRY
        # fallback in client.py. Without it, changing that fallback to a
        # disabled policy would leave the whole suite green while silently
        # removing retries for every consumer who does not pass `retry`.
        # Patch the backoff, not the policy: the policy is what is under test.
        monkeypatch.setattr("huuray.client.backoff_delay", lambda *_a, **_k: 0.0)
        transport = RecordingTransport(
            [MockResponse(status=503), MockResponse(json={"Balances": []})]
        )
        client = HuurayClient(
            api_token="test-token",
            api_secret="test-secret",
            transport=httpx.MockTransport(transport.handle_request),
        )
        client.balances.list()
        assert len(transport.calls) == 2, "the default retry policy must retry a read"


class TestTransportFaultsOnTheResponseBody:
    def test_maps_a_mid_body_connection_drop_into_the_taxonomy_not_a_raw_httpx_error(self):
        client, _ = make_client(MockResponse(body_raises=httpx.ReadError("terminated")))
        with pytest.raises(HuurayConnectionError):
            client.balances.list()

    def test_maps_a_mid_body_timeout_to_huuray_timeout_error(self):
        client, _ = make_client(MockResponse(body_raises=httpx.ReadTimeout("timed out")))
        with pytest.raises(HuurayTimeoutError):
            client.balances.list()

    def test_maps_a_pre_headers_timeout_to_huuray_timeout_error(self):
        client, _ = make_client(MockResponse(raises=httpx.ConnectTimeout("timed out")))
        with pytest.raises(HuurayTimeoutError) as caught:
            client.balances.list()
        assert "timed out after" in str(caught.value)

    def test_treats_a_garbled_200_body_as_a_transport_fault_never_as_an_empty_result(self):
        # An empty result from a garbled /v4/Search response would tell the
        # reconciliation flow "the order did not land" — inviting a double order.
        client, _ = make_client(MockResponse(status=200, text="<html>gateway error</html>"))
        with pytest.raises(HuurayConnectionError):
            client.orders.search(ref_id="r")

    def test_treats_an_empty_200_body_the_same_way(self):
        client, _ = make_client(MockResponse(status=200, text=""))
        with pytest.raises(HuurayConnectionError, match="empty"):
            client.balances.list()

    def test_never_quotes_the_body_in_the_error_it_could_hold_a_code(self):
        client, _ = make_client(MockResponse(status=200, text="LEAKED-CODE-123 <not json>"))
        with pytest.raises(HuurayConnectionError) as caught:
            client.balances.list()
        assert "LEAKED-CODE-123" not in str(caught.value)

    def test_retries_a_retryable_read_after_a_garbled_body(self):
        client, calls = make_client(
            [
                MockResponse(status=200, text="not json"),
                MockResponse(status=200, json={"Balances": []}),
            ],
            retry=RetryOptions(max_retries=2, base_delay=0.001),
        )
        assert client.balances.list().balances == []
        assert len(calls) == 2


class TestRequestEscapeHatch:
    def test_calls_any_endpoint_with_signing_handled(self):
        client, calls = make_client(MockResponse(json={"OrderUID": "abc"}))
        out = client.request("POST", "/v4/Search", {"RefID": "payroll-2026-08-jane"})
        assert out["OrderUID"] == "abc"
        assert calls[0].body == {"RefID": "payroll-2026-08-jane"}
        assert calls[0].headers["x-api-hash"]

    def test_does_not_retry_unless_asked_to(self):
        client, calls = make_client(
            MockResponse(status=503), retry=RetryOptions(max_retries=3, base_delay=0.001)
        )
        with pytest.raises(HuurayServerError):
            client.request("POST", "/v4/Search", {})
        assert len(calls) == 1

    @pytest.mark.parametrize(
        "method",
        ["GET\r\nX-Injected: yes", "GE T", "GET\x00", "G\tET", "", "GÉT", "GET/", "(GET)"],
    )
    def test_rejects_a_method_that_is_not_an_http_token_before_sending(self, method):
        # Left to httpx, these failed at send time as HuurayConnectionError quoting
        # the method, or escaped as a raw TypeError for a non-ASCII one.
        client, calls = make_client()
        with pytest.raises(ValueError, match="HTTP method must be a token") as caught:
            client.request(method, "/v4/Balance")
        assert calls == []
        if method:
            assert method not in str(caught.value)

    def test_accepts_a_lowercase_method_token_and_sends_it_uppercased(self):
        client, calls = make_client()
        client.request("get", "/v4/Balance")
        assert calls[0].method == "GET"

    @pytest.mark.parametrize(
        "path",
        [
            # Appended to the base URL as a string: "@evil.example" became the host,
            # with the base host sent as Basic credentials; ":8443" became the port;
            # ".evil.example" and "v4/..." extended the host name.
            "@evil.example/v4/Order",
            ".evil.example/v4/Balance",
            "http://evil.example/v4/Balance",
            ":8443/v4/Balance",
            "v4/Balance",
            "/v4/Balance\r\nX-Injected: yes",
            "/v4/Ba lance",
            "/v4/Balance\x00",
            "/v4/\u2028",
            "",
        ],
    )
    def test_rejects_a_path_that_could_move_the_host_or_is_not_visible_ascii(self, path):
        client, calls = make_client()
        with pytest.raises(ValueError, match='the path must start with "/"') as caught:
            client.request("GET", path)
        assert calls == []
        assert "evil" not in str(caught.value)
        assert "Injected" not in str(caught.value)

    def test_a_path_starting_with_two_slashes_stays_on_the_configured_host(self):
        # Appended as a string, "//" cannot reach the authority, so it is accepted.
        client, calls = make_client()
        client.request("GET", "//evil.example/v4/Balance")
        assert calls[0].origin == "https://api.huuray.com"
        assert calls[0].path == "//evil.example/v4/Balance"


class TestAsyncClient:
    async def test_reads_work_and_send_the_same_headers(self):
        client, calls = make_async_client(MockResponse(json={"Balances": []}))
        async with client:
            assert (await client.balances.list()).balances == []
        assert calls[0].headers["x-api-token"] == "test-token"
        assert re.fullmatch(r"[0-9a-f]{128}", calls[0].headers["x-api-hash"])

    async def test_maps_errors_through_the_same_taxonomy(self):
        client, _ = make_async_client(MockResponse(status=404, json={"Status": 404}))
        async with client:
            with pytest.raises(HuurayNotFoundError):
                await client.templates.list()

    async def test_maps_a_mid_body_drop_into_the_taxonomy(self):
        client, _ = make_async_client(MockResponse(body_raises=httpx.ReadError("terminated")))
        async with client:
            with pytest.raises(HuurayConnectionError):
                await client.balances.list()

    async def test_treats_a_garbled_200_body_as_a_transport_fault(self):
        client, _ = make_async_client(MockResponse(status=200, text="nope"))
        async with client:
            with pytest.raises(HuurayConnectionError):
                await client.orders.search(ref_id="r")

    async def test_retries_reads_but_never_orders(self):
        client, calls = make_async_client(
            MockResponse(status=503), retry=RetryOptions(max_retries=3, base_delay=0.001)
        )
        async with client:
            with pytest.raises(HuurayServerError):
                await client.orders.resend(order_uid="x")
        assert len(calls) == 1

    async def test_the_escape_hatch_is_awaited(self):
        client, calls = make_async_client(MockResponse(json={"OrderUID": "abc"}))
        async with client:
            out = await client.request("POST", "/v4/Search", {"RefID": "r"})
        assert out["OrderUID"] == "abc"
        assert calls[0].path == "/v4/Search"

    async def test_the_escape_hatch_rejects_an_unsafe_method_or_path_before_sending(self):
        client, calls = make_async_client()
        async with client:
            with pytest.raises(ValueError, match="HTTP method must be a token"):
                await client.request("GET\r\nX-Injected: yes", "/v4/Balance")
            with pytest.raises(ValueError, match='the path must start with "/"'):
                await client.request("GET", "@evil.example/v4/Order")
            with pytest.raises(ValueError, match='the path must start with "/"'):
                await client.request("GET", ":8443/v4/Balance")
        assert calls == []

    async def test_an_unsendable_nonce_on_an_order_is_a_value_error_not_an_indeterminate_order(
        self,
    ):
        client, calls = make_async_client(nonce_factory=lambda: "")
        async with client:
            with pytest.raises(ValueError, match="X-API-NONCE"):
                await client.orders.create(
                    product_token="t", value=100, currency="DKK", quantity=1, ref_id="r"
                )
        assert calls == []
