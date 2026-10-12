"""Construction, signing, error mapping, retries, and transport faults."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable

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
    HuurayRateLimitError,
    HuurayServerError,
    HuurayTimeoutError,
    HuurayValidationError,
    RawResponse,
    Recipient,
    RetryOptions,
)
from huuray.auth import sign_request
from huuray.resources._base import Operation
from huuray.retry import backoff_delay

from .helpers import (
    MockResponse,
    RecordingTransport,
    make_async_client,
    make_client,
    rate_limited,
    record_retry_sleeps,
)

#: The longest ``RetryOptions.max_delay`` kept, in seconds.
LONGEST_DELAY = 2_147_483.647


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
        assert str(caught.value).count("https://api.huuray.com") == 1  # the expected example
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
        assert str(caught.value).count("https://api.huuray.com") == 1  # the expected example
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
        assert str(caught.value).count("https://api.huuray.com") == 1  # the expected example
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
            "https://127.0.0.1:99999",
            "http://127.0.0.1:0",
            "http://leaky.example.test:65536",
            "http://:80",
            # A raw idna.IDNAError at the first request.
            "http://xn--",
            # A raw UnicodeError from the sync transport at the first request.
            "http://a..b",
            "https://.",
            "https://" + "a" * 64 + ".example.test",
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
        assert str(caught.value).count("https://api.huuray.com") == 1  # the expected example
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
            (429, HuurayRateLimitError),
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

    @pytest.mark.parametrize(
        ("given", "kept"),
        [
            (float("inf"), LONGEST_DELAY),
            (1e10, LONGEST_DELAY),
            (2_147_483.648, LONGEST_DELAY),
            (LONGEST_DELAY, LONGEST_DELAY),
            (30, 30.0),
            (-1, 0.0),
            (float("nan"), 0.0),
        ],
    )
    def test_clamps_max_delay_to_what_a_sleep_honours_on_every_platform(self, given, kept):
        # 2147483647 ms. Above 4294967 seconds, time.sleep() raises OverflowError
        # on Windows with Python 3.9.
        assert RetryOptions(max_delay=given).max_delay == kept

    def test_the_backoff_stays_within_the_clamped_max_delay(self, monkeypatch):
        monkeypatch.setattr("huuray.retry.random.random", lambda: 1.0)
        unbounded = RetryOptions(base_delay=float("inf"), max_delay=float("inf"))
        assert backoff_delay(0, unbounded) == LONGEST_DELAY

    @pytest.mark.parametrize("attempt", [1023, 1024, 5000])
    def test_the_backoff_of_a_very_late_attempt_is_max_delay_not_an_overflow(
        self, monkeypatch, attempt
    ):
        # float(2**1024) raises OverflowError.
        monkeypatch.setattr("huuray.retry.random.random", lambda: 1.0)
        assert backoff_delay(attempt, RetryOptions(max_retries=6000)) == 4.0
        assert backoff_delay(attempt, RetryOptions(max_retries=6000, base_delay=0)) == 0.0

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


#: What the jittered backoff is replaced with, to tell it from a ``Retry-After``.
BACKOFF = 0.125

#: Two retries, waiting at most the default ``max_delay`` of 4 seconds.
RETRYING = RetryOptions(max_retries=2)

BALANCES = MockResponse(json={"Balances": []})


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits between the sync client's attempts, recorded instead of slept."""
    monkeypatch.setattr("huuray.client.backoff_delay", lambda *_a, **_k: BACKOFF)
    return record_retry_sleeps(monkeypatch, awaited=False)


@pytest.fixture
def async_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The same for the async client, failing if it blocks instead of awaiting."""
    monkeypatch.setattr("huuray.client.backoff_delay", lambda *_a, **_k: BACKOFF)
    return record_retry_sleeps(monkeypatch, awaited=True)


#: Every operation the client repeats.
READS: dict[str, Callable[[Any], Any]] = {
    "balances": lambda c: c.balances.list(),
    "catalogue": lambda c: c.catalogue.list(),
    "templates": lambda c: c.templates.list(),
    "stock": lambda c: c.stock.check(product_token="t"),
    "exchange_rates": lambda c: c.exchange_rates.get(from_currency="DKK", to_currency="EUR"),
    "search": lambda c: c.orders.search(ref_id="r"),
    "pdf": lambda c: c.pdfs.get(order_uid="x"),
}

ORDER: dict[str, Any] = {"product_token": "t", "value": 100, "currency": "DKK", "ref_id": "r"}

#: Every operation the client never repeats.
WRITES: dict[str, Callable[[Any], Any]] = {
    "create": lambda c: c.orders.create(**ORDER, quantity=1),
    "create_sync": lambda c: c.orders.create_sync(**ORDER, quantity=1),
    "send_reward": lambda c: c.send_reward(
        **ORDER, recipient=Recipient(email="jane@example.com"), template_id=42
    ),
    "resend": lambda c: c.orders.resend(order_uid="x"),
    "cancel": lambda c: c.orders.cancel(order_uid="x"),
    "upload": lambda c: c.uploads.create(file=b"%PDF-1.7", file_name="po.pdf"),
}


class TestRateLimitMapping:
    """A 429: the rate limit was exceeded and the request was not processed."""

    def test_a_429_is_a_rate_limit_error_carrying_retry_after(self):
        client, calls = make_client(rate_limited("30"))
        with pytest.raises(HuurayRateLimitError) as caught:
            client.balances.list()
        error = caught.value
        assert type(error) is HuurayRateLimitError
        assert isinstance(error, HuurayAPIError)
        assert error.retry_after == 30
        assert (error.http_status, error.status) == (429, 429)
        assert error.status_message == "Too Many Requests"
        assert (error.method, error.path) == ("GET", "/v4/Balance")
        assert len(calls) == 1

    def test_retry_after_is_none_without_the_header(self):
        client, _ = make_client(rate_limited(None))
        with pytest.raises(HuurayRateLimitError) as caught:
            client.balances.list()
        assert caught.value.retry_after is None

    @pytest.mark.parametrize(
        ("header", "seconds"),
        [
            ("0", 0),
            ("120", 120),
            (" 45 ", 45),
            ("", None),
            ("soon", None),
            ("-5", None),
            ("1.5", None),
            ("30s", None),
            ("+30", None),
            ("Wed, 21 Oct 2026 07:28:00 GMT", None),
            ("9" * 400, int("9" * 400)),
        ],
        ids=lambda value: f"{len(str(value))}-digits" if len(str(value)) > 40 else None,
    )
    def test_reads_retry_after_as_whole_seconds_or_none_like_the_pdf_answer(self, header, seconds):
        client, _ = make_client(rate_limited(header))
        with pytest.raises(HuurayRateLimitError) as caught:
            client.balances.list()
        assert caught.value.retry_after == seconds

    def test_reads_retry_after_from_a_429_whose_body_is_not_json(self):
        client, _ = make_client(
            MockResponse(status=429, text="<html>slow down</html>", headers={"Retry-After": "7"})
        )
        with pytest.raises(HuurayRateLimitError) as caught:
            client.balances.list()
        assert caught.value.retry_after == 7
        assert caught.value.status_message is None

    def test_from_response_without_headers_has_no_retry_after(self):
        error = HuurayAPIError.from_response(429, {"Status": 429}, "GET", "/v4/Balance")
        assert type(error) is HuurayRateLimitError
        assert error.retry_after is None

    @pytest.mark.parametrize("name", ["Retry-After", "retry-after", "RETRY-AFTER"])
    def test_from_response_reads_retry_after_from_a_plain_dict_whatever_its_case(self, name):
        error = HuurayAPIError.from_response(
            429, {"Status": 429}, "GET", "/v4/Balance", {"Content-Type": "text/html", name: "5"}
        )
        assert type(error) is HuurayRateLimitError
        assert error.retry_after == 5

    def test_from_response_without_a_retry_after_among_the_headers_has_none(self):
        error = HuurayAPIError.from_response(
            429, {"Status": 429}, "GET", "/v4/Balance", {"Retry-After-Hint": "5"}
        )
        assert isinstance(error, HuurayRateLimitError)
        assert error.retry_after is None

    def test_retry_after_is_read_for_a_429_only(self):
        client, _ = make_client(MockResponse(status=503, headers={"Retry-After": "7"}))
        with pytest.raises(HuurayServerError) as caught:
            client.balances.list()
        assert not hasattr(caught.value, "retry_after")

    async def test_the_async_client_maps_a_429_the_same_way(self):
        client, calls = make_async_client(rate_limited("30"))
        async with client:
            with pytest.raises(HuurayRateLimitError) as caught:
                await client.balances.list()
        assert type(caught.value) is HuurayRateLimitError
        assert caught.value.retry_after == 30
        assert len(calls) == 1


class TestRateLimitedReads:
    """A read answered 429 waits ``Retry-After`` instead of the backoff, within ``max_delay``."""

    def test_every_wait_goes_through_time_sleep_as_the_caller_patched_it(self, monkeypatch):
        # Looked up when the wait is taken: a caller who patches time.sleep in
        # their own tests still sees the waits, and never waits for real.
        monkeypatch.setattr("huuray.client.backoff_delay", lambda *_a, **_k: BACKOFF)
        waited: list[float] = []
        monkeypatch.setattr(time, "sleep", waited.append)
        client, calls = make_client(
            [rate_limited("2"), MockResponse(status=503), BALANCES], retry=RETRYING
        )
        client.balances.list()
        assert waited == [2, BACKOFF]
        assert len(calls) == 3

    def test_waits_retry_after_instead_of_the_backoff_and_signs_again(self, sleeps):
        client, calls = make_client([rate_limited("2"), BALANCES], retry=RETRYING)
        assert client.balances.list().balances == []
        assert sleeps == [2]
        assert len(calls) == 2
        assert len({call.headers["x-api-nonce"] for call in calls}) == 2

    @pytest.mark.parametrize("read", READS.values(), ids=READS)
    def test_every_operation_the_client_repeats_waits_retry_after(self, sleeps, read):
        client, calls = make_client(
            [rate_limited("3"), MockResponse(status=200, json={})], retry=RETRYING
        )
        read(client)
        assert sleeps == [3]
        assert len(calls) == 2

    def test_a_retry_after_of_zero_still_waits_one_second(self, sleeps):
        client, calls = make_client([rate_limited("0"), BALANCES], retry=RETRYING)
        client.balances.list()
        assert sleeps == [1]
        assert len(calls) == 2

    @pytest.mark.parametrize("header", [None, "soon", "1.5", "Wed, 21 Oct 2026 07:28:00 GMT"])
    def test_backs_off_as_before_without_a_usable_retry_after(self, sleeps, header):
        client, calls = make_client([rate_limited(header), BALANCES], retry=RETRYING)
        client.balances.list()
        assert sleeps == [BACKOFF]
        assert len(calls) == 2

    @pytest.mark.parametrize(
        "next_failure",
        [
            MockResponse(status=503),
            MockResponse(raises=httpx.ConnectError("refused")),
            MockResponse(status=200, text="not json"),
            rate_limited(None),
        ],
        ids=["503", "connection-failure", "garbled-body", "429-without-retry-after"],
    )
    def test_a_retry_after_replaces_only_the_wait_that_follows_it(self, sleeps, next_failure):
        client, calls = make_client([rate_limited("3"), next_failure, BALANCES], retry=RETRYING)
        client.balances.list()
        assert sleeps == [3, BACKOFF]
        assert len(calls) == 3

    def test_a_retry_after_equal_to_max_delay_is_waited(self, sleeps):
        client, calls = make_client([rate_limited("4"), BALANCES], retry=RETRYING)
        client.balances.list()
        assert sleeps == [4]
        assert len(calls) == 2

    @pytest.mark.parametrize(
        "header", ["5", "3600", "9" * 400], ids=["just-over", "an-hour", "past-float"]
    )
    def test_a_retry_after_longer_than_max_delay_is_raised_at_once(self, sleeps, header):
        # The caller decides whether to wait that long. Never an OverflowError.
        client, calls = make_client([rate_limited(header)], retry=RETRYING)
        with pytest.raises(HuurayRateLimitError) as caught:
            client.balances.list()
        assert caught.value.retry_after == int(header)
        assert sleeps == []
        assert len(calls) == 1

    def test_a_longer_max_delay_waits_a_longer_retry_after(self, sleeps):
        client, calls = make_client(
            [rate_limited("30"), BALANCES], retry=RetryOptions(max_retries=1, max_delay=30)
        )
        client.balances.list()
        assert sleeps == [30]
        assert len(calls) == 2

    def test_a_retry_after_too_long_for_any_sleep_is_raised_whatever_the_max_delay(self):
        # The real time.sleep(): it raises OverflowError for a wait this long.
        client, calls = make_client(
            [rate_limited("9" * 400)], retry=RetryOptions(max_retries=2, max_delay=float("inf"))
        )
        with pytest.raises(HuurayRateLimitError) as caught:
            client.balances.list()
        assert caught.value.retry_after == int("9" * 400)
        assert len(calls) == 1

    def test_the_longest_max_delay_is_still_the_longest_retry_after_waited(self, sleeps):
        unbounded = RetryOptions(max_retries=1, max_delay=float("inf"))
        client, calls = make_client([rate_limited("2147483"), BALANCES], retry=unbounded)
        client.balances.list()
        assert sleeps == [2147483]
        assert len(calls) == 2

        client, calls = make_client([rate_limited("2147484")], retry=unbounded)
        with pytest.raises(HuurayRateLimitError):
            client.balances.list()
        assert sleeps == [2147483]
        assert len(calls) == 1

    def test_the_one_second_floor_is_what_must_fit_max_delay(self, sleeps):
        client, calls = make_client(
            [rate_limited("0")], retry=RetryOptions(max_retries=2, max_delay=0.5)
        )
        with pytest.raises(HuurayRateLimitError):
            client.balances.list()
        assert sleeps == []
        assert len(calls) == 1

    def test_raises_the_rate_limit_error_when_the_retries_are_used_up(self, sleeps):
        client, calls = make_client([rate_limited("1")] * 3, retry=RETRYING)
        with pytest.raises(HuurayRateLimitError) as caught:
            client.balances.list()
        assert caught.value.retry_after == 1
        assert sleeps == [1, 1]
        assert len(calls) == 3

    def test_is_raised_at_once_when_retrying_is_disabled(self, sleeps):
        client, calls = make_client([rate_limited("1")], retry=RetryOptions(max_retries=0))
        with pytest.raises(HuurayRateLimitError):
            client.balances.list()
        assert sleeps == []
        assert len(calls) == 1

    def test_the_escape_hatch_waits_only_when_marked_retryable(self, sleeps):
        client, calls = make_client(
            [rate_limited("2"), MockResponse(json={"OrderUID": "abc"}), rate_limited("2")],
            retry=RETRYING,
        )
        client.request("POST", "/v4/Search", {"RefID": "r"}, retryable=True)
        with pytest.raises(HuurayRateLimitError):
            client.request("POST", "/v4/Search", {"RefID": "r"})
        assert sleeps == [2]
        assert len(calls) == 3


class TestRateLimitedWrites:
    """Never repeated, and never indeterminate: a 429 was not processed."""

    @pytest.mark.parametrize("write", WRITES.values(), ids=WRITES)
    def test_a_429_is_raised_as_it_is_after_one_attempt(self, sleeps, write):
        client, calls = make_client([rate_limited("1")], retry=RetryOptions(max_retries=3))
        with pytest.raises(HuurayRateLimitError) as caught:
            write(client)
        assert type(caught.value) is HuurayRateLimitError
        assert caught.value.retry_after == 1
        assert sleeps == []
        assert len(calls) == 1

    @pytest.mark.parametrize("write", WRITES.values(), ids=WRITES)
    async def test_the_async_client_raises_it_after_one_attempt_too(self, async_sleeps, write):
        client, calls = make_async_client([rate_limited("1")], retry=RetryOptions(max_retries=3))
        async with client:
            with pytest.raises(HuurayRateLimitError) as caught:
                await write(client)
        assert type(caught.value) is HuurayRateLimitError
        assert caught.value.retry_after == 1
        assert async_sleeps == []
        assert len(calls) == 1


class TestAsyncRateLimitedReads:
    async def test_every_wait_goes_through_asyncio_sleep_as_the_caller_patched_it(
        self, monkeypatch
    ):
        monkeypatch.setattr("huuray.client.backoff_delay", lambda *_a, **_k: BACKOFF)
        waited: list[float] = []

        async def patched_sleep(seconds: float) -> None:
            waited.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", patched_sleep)
        client, calls = make_async_client(
            [rate_limited("2"), MockResponse(status=503), BALANCES], retry=RETRYING
        )
        async with client:
            await client.balances.list()
        assert waited == [2, BACKOFF]
        assert len(calls) == 3

    async def test_awaits_retry_after_instead_of_the_backoff_and_signs_again(self, async_sleeps):
        client, calls = make_async_client([rate_limited("2"), BALANCES], retry=RETRYING)
        async with client:
            assert (await client.balances.list()).balances == []
        assert async_sleeps == [2]
        assert len(calls) == 2
        assert len({call.headers["x-api-nonce"] for call in calls}) == 2

    async def test_a_retry_after_too_long_for_any_sleep_is_raised_whatever_the_max_delay(self):
        # The real asyncio.sleep(): it raises OverflowError for a wait this long.
        client, calls = make_async_client(
            [rate_limited("9" * 400)], retry=RetryOptions(max_retries=2, max_delay=float("inf"))
        )
        async with client:
            with pytest.raises(HuurayRateLimitError) as caught:
                await client.balances.list()
        assert caught.value.retry_after == int("9" * 400)
        assert len(calls) == 1

    async def test_the_longest_max_delay_is_still_the_longest_retry_after_waited(
        self, async_sleeps
    ):
        unbounded = RetryOptions(max_retries=1, max_delay=float("inf"))
        client, calls = make_async_client([rate_limited("2147483"), BALANCES], retry=unbounded)
        async with client:
            await client.balances.list()
        assert async_sleeps == [2147483]
        assert len(calls) == 2

        client, calls = make_async_client([rate_limited("2147484")], retry=unbounded)
        async with client:
            with pytest.raises(HuurayRateLimitError):
                await client.balances.list()
        assert async_sleeps == [2147483]
        assert len(calls) == 1

    @pytest.mark.parametrize("read", READS.values(), ids=READS)
    async def test_every_operation_the_client_repeats_awaits_retry_after(self, async_sleeps, read):
        client, calls = make_async_client(
            [rate_limited("3"), MockResponse(status=200, json={})], retry=RETRYING
        )
        async with client:
            await read(client)
        assert async_sleeps == [3]
        assert len(calls) == 2

    async def test_a_retry_after_of_zero_still_waits_one_second(self, async_sleeps):
        client, calls = make_async_client([rate_limited("0"), BALANCES], retry=RETRYING)
        async with client:
            await client.balances.list()
        assert async_sleeps == [1]
        assert len(calls) == 2

    async def test_backs_off_without_retry_after_then_waits_the_next_one(self, async_sleeps):
        client, calls = make_async_client(
            [rate_limited(None), rate_limited("3"), BALANCES], retry=RETRYING
        )
        async with client:
            await client.balances.list()
        assert async_sleeps == [BACKOFF, 3]
        assert len(calls) == 3

    async def test_a_retry_after_replaces_only_the_wait_that_follows_it(self, async_sleeps):
        client, calls = make_async_client(
            [rate_limited("3"), MockResponse(raises=httpx.ConnectError("refused")), BALANCES],
            retry=RETRYING,
        )
        async with client:
            await client.balances.list()
        assert async_sleeps == [3, BACKOFF]
        assert len(calls) == 3

    @pytest.mark.parametrize("header", ["5", "9" * 400], ids=["just-over", "past-float"])
    async def test_a_retry_after_longer_than_max_delay_is_raised_at_once(
        self, async_sleeps, header
    ):
        client, calls = make_async_client([rate_limited(header)], retry=RETRYING)
        async with client:
            with pytest.raises(HuurayRateLimitError) as caught:
                await client.balances.list()
        assert caught.value.retry_after == int(header)
        assert async_sleeps == []
        assert len(calls) == 1

    async def test_raises_the_rate_limit_error_when_the_retries_are_used_up(self, async_sleeps):
        client, calls = make_async_client([rate_limited("1")] * 3, retry=RETRYING)
        async with client:
            with pytest.raises(HuurayRateLimitError) as caught:
                await client.balances.list()
        assert caught.value.retry_after == 1
        assert async_sleeps == [1, 1]
        assert len(calls) == 3

    async def test_awaits_the_real_asyncio_sleep_without_blocking(self, monkeypatch):
        # Retry-After: 0 is floored to a real one-second wait, the shortest there is.
        def blocking_sleep(seconds: float) -> None:
            raise AssertionError("the async client blocked the event loop with time.sleep()")

        monkeypatch.setattr("huuray.client._sleep", blocking_sleep)
        client, calls = make_async_client([rate_limited("0"), BALANCES], retry=RETRYING)
        started = time.monotonic()
        async with client:
            await client.balances.list()
        assert time.monotonic() - started >= 0.9
        assert len(calls) == 2


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


class TestRawResponse:
    def test_the_headers_change_neither_equality_nor_hashing(self):
        # As before the headers were added: httpx.Headers is unhashable, and two
        # answers differing only in, say, Date would otherwise compare unequal.
        with_headers = RawResponse(
            data="ok", http_status=200, headers=httpx.Headers({"Retry-After": "1"})
        )
        without = RawResponse(data="ok", http_status=200)
        assert with_headers == without
        assert hash(with_headers) == hash(without)
        assert with_headers != RawResponse(data="ok", http_status=202)


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
