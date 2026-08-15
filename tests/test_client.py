"""Construction, signing, error mapping, retries, and transport faults."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from huuray import (
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

from .helpers import (
    MockResponse,
    RecordingTransport,
    make_async_client,
    make_client,
)


class TestConstruction:
    def test_requires_an_api_token(self):
        with pytest.raises(HuurayConfigError):
            HuurayClient(api_token="", api_secret="s")

    def test_requires_an_api_secret(self):
        with pytest.raises(HuurayConfigError):
            HuurayClient(api_token="t", api_secret="")

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

    @pytest.mark.parametrize("good", ["https://api.huuray.com", "http://localhost:8080"])
    def test_accepts_an_absolute_http_base_url(self, good):
        assert HuurayClient(api_token="t", api_secret="s", base_url=good) is not None

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
