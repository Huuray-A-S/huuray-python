"""Gift card PDFs: the request, 200 and 202, the polling helper, and keeping the PDF out of dumps."""

from __future__ import annotations

import asyncio
import base64
import logging
import pprint
import time
from typing import Any, Optional

import httpx
import pytest

from huuray import (
    HuurayAPIError,
    HuurayAuthError,
    HuurayClient,
    HuurayConnectionError,
    HuurayNotFoundError,
    HuurayServerError,
    HuurayTimeoutError,
    HuurayValidationError,
    PdfDocument,
    PdfResult,
    RetryOptions,
    redact,
    safe_json,
)
from huuray.auth import sign_request
from huuray.redact import BEARER_MARKER
from huuray.resources import pdfs as pdfs_module

from .helpers import MockResponse, RecordingTransport, make_async_client, make_client

ORDER_UID = "0f8a3c52-1d6e-4b7a-9c2f-5e4d3b2a1c90"
TEMPLATE_UID = "c7d1e2f3-4a5b-4c6d-8e9f-0a1b2c3d4e5f"

#: Every byte value, so a decode that drops or alters one is caught.
PDF_ONE = b"%PDF-1.7\n" + bytes(range(256)) + b"\n%%EOF 5123401"
PDF_TWO = b"%PDF-1.7\n" + bytes(range(255, -1, -1)) + b"\n%%EOF 5123402"
COMBINED = PDF_ONE + PDF_TWO

STILL_PROCESSING = "The order is still being processed, retry in 30 seconds"

RETRYING = RetryOptions(max_retries=2, base_delay=0.001)


def b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def document(voucher_id: int, content: bytes) -> dict[str, Any]:
    return {
        "VoucherIDs": [voucher_id],
        "PDFTemplateUid": TEMPLATE_UID,
        "FileName": f"giftcard-{voucher_id}.pdf",
        "ContentType": "application/pdf",
        "Content": b64(content),
    }


def envelope(status: int, documents: list[dict[str, Any]], message: str = "OK") -> dict[str, Any]:
    return {
        "OrderUID": ORDER_UID,
        "Documents": documents,
        "Status": status,
        "Message": message,
        "StatusMessage": message,
    }


READY = envelope(200, [document(5123401, PDF_ONE), document(5123402, PDF_TWO)])

COMBINED_READY = envelope(
    200,
    [
        {
            "VoucherIDs": [5123401, 5123402],
            "PDFTemplateUid": None,
            "FileName": f"giftcard-order-{ORDER_UID}.pdf",
            "ContentType": "application/pdf",
            "Content": b64(COMBINED),
        }
    ],
)

READY_RESULT = PdfResult(
    ready=True,
    order_uid=ORDER_UID,
    documents=[
        PdfDocument(
            voucher_ids=[5123401],
            pdf_template_uid=TEMPLATE_UID,
            file_name="giftcard-5123401.pdf",
            content_type="application/pdf",
            content=PDF_ONE,
        ),
        PdfDocument(
            voucher_ids=[5123402],
            pdf_template_uid=TEMPLATE_UID,
            file_name="giftcard-5123402.pdf",
            content_type="application/pdf",
            content=PDF_TWO,
        ),
    ],
    retry_after=None,
)


def ready(body: Optional[dict[str, Any]] = None) -> MockResponse:
    return MockResponse(status=200, json=READY if body is None else body)


def not_ready(retry_after: Optional[str] = "30", message: str = STILL_PROCESSING) -> MockResponse:
    return MockResponse(
        status=202,
        json=envelope(202, [], message),
        headers={} if retry_after is None else {"Retry-After": retry_after},
    )


def nonces(calls: list[Any]) -> list[str]:
    return [call.headers["x-api-nonce"] for call in calls]


class FakeClock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)


def blocking_sleep(seconds: float) -> None:
    raise AssertionError("the async client blocked the event loop with time.sleep()")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replaces the clock and sleeps get_when_ready() uses, as the backoff is replaced."""
    fake = FakeClock()
    monkeypatch.setattr("huuray.resources.pdfs._monotonic", fake.monotonic)
    monkeypatch.setattr("huuray.resources.pdfs._sleep", fake.sleep)
    monkeypatch.setattr("huuray.resources.pdfs._async_sleep", fake.async_sleep)
    return fake


@pytest.fixture
def async_clock(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The same clock, with the blocking sleep made to fail if the async client uses it."""
    monkeypatch.setattr("huuray.resources.pdfs._sleep", blocking_sleep)
    return clock


def client_on_clock(
    clock: FakeClock, responses: list[MockResponse], seconds_per_call: float
) -> tuple[HuurayClient, list[Any]]:
    """A client whose every request takes ``seconds_per_call`` on the fake clock."""
    transport = RecordingTransport(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        clock.now += seconds_per_call
        return transport.handle_request(request)

    client = HuurayClient(
        api_token="test-token",
        api_secret="test-secret",
        transport=httpx.MockTransport(handle),
        retry=RetryOptions(max_retries=0),
    )
    return client, transport.calls


class TestTheRequest:
    def test_is_one_signed_json_post_to_v4_pdf_with_only_the_order_uid(self):
        client, calls = make_client(ready(), nonce_factory=lambda: "fixed-nonce")
        client.pdfs.get(order_uid=ORDER_UID)

        assert len(calls) == 1
        call = calls[0]
        assert (call.method, call.origin, call.path, call.query) == (
            "POST",
            "https://api.huuray.com",
            "/v4/Pdf",
            {},
        )
        assert call.headers["x-api-token"] == "test-token"
        assert call.headers["x-api-nonce"] == "fixed-nonce"
        assert call.headers["x-api-hash"] == sign_request("test-secret", "fixed-nonce")
        assert call.headers["content-type"] == "application/json"
        assert call.headers["accept"] == "application/json"
        # The optional fields are omitted, not sent as null.
        assert call.body == {"OrderUID": ORDER_UID}

    def test_sends_every_optional_field_when_given(self):
        client, calls = make_client(ready())
        client.pdfs.get(
            order_uid=ORDER_UID, voucher_id=5123402, pdf_template_uid=TEMPLATE_UID, combine=True
        )
        assert calls[0].body == {
            "OrderUID": ORDER_UID,
            "VoucherID": 5123402,
            "PDFTemplateUid": TEMPLATE_UID,
            "Combine": True,
        }

    def test_sends_combine_false_when_it_is_given(self):
        client, calls = make_client(ready())
        client.pdfs.get(order_uid=ORDER_UID, combine=False)
        assert calls[0].body == {"OrderUID": ORDER_UID, "Combine": False}

    def test_leaves_every_check_to_the_api(self):
        # No GUID check and no receiver count: the API decides, with a 400 or 422.
        client, calls = make_client(ready())
        client.pdfs.get(order_uid=" not-a-guid ", voucher_id=-1, pdf_template_uid="nope")
        assert calls[0].body == {
            "OrderUID": " not-a-guid ",
            "VoucherID": -1,
            "PDFTemplateUid": "nope",
        }

    async def test_the_async_client_sends_the_same_requests(self):
        sync_client, sync_calls = make_client(ready())
        async_client, async_calls = make_async_client(ready())
        arguments_list: list[dict[str, Any]] = [
            {"order_uid": ORDER_UID},
            {
                "order_uid": ORDER_UID,
                "voucher_id": 1,
                "pdf_template_uid": TEMPLATE_UID,
                "combine": True,
            },
        ]
        async with async_client:
            for arguments in arguments_list:
                sync_client.pdfs.get(**arguments)
                await async_client.pdfs.get(**arguments)

        def shape(call: Any) -> tuple[Any, ...]:
            return (call.method, call.path, call.media_type, call.content)

        assert [shape(c) for c in async_calls] == [shape(c) for c in sync_calls]


class TestAReadyAnswer:
    def test_a_200_is_ready_and_decodes_every_document_to_the_exact_bytes(self):
        client, _ = make_client(ready())
        result = client.pdfs.get(order_uid=ORDER_UID)
        assert result == READY_RESULT
        assert [doc.content for doc in result.documents] == [PDF_ONE, PDF_TWO]

    def test_maps_a_combined_document_with_several_voucher_ids_and_no_template(self):
        client, _ = make_client(ready(COMBINED_READY))
        result = client.pdfs.get(order_uid=ORDER_UID, combine=True)
        assert result == PdfResult(
            ready=True,
            order_uid=ORDER_UID,
            documents=[
                PdfDocument(
                    voucher_ids=[5123401, 5123402],
                    pdf_template_uid=None,
                    file_name=f"giftcard-order-{ORDER_UID}.pdf",
                    content_type="application/pdf",
                    content=COMBINED,
                )
            ],
        )

    @pytest.mark.parametrize(
        ("body", "documents"),
        [
            ({"Status": 200}, []),
            ({"OrderUID": None, "Documents": None}, []),
            (
                {"Documents": [{"VoucherIDs": None, "Content": None}]},
                [PdfDocument([], None, None, None, None)],
            ),
            ({"Documents": [{"Content": ""}]}, [PdfDocument([], None, None, None, b"")]),
        ],
        ids=["absent", "null", "null-fields", "empty-content"],
    )
    def test_maps_absent_or_null_fields_to_none_or_empty(self, body, documents):
        client, _ = make_client(ready(body))
        result = client.pdfs.get(order_uid=ORDER_UID)
        assert result == PdfResult(ready=True, order_uid=None, documents=documents)

    def test_reads_a_response_body_over_20_mb_in_full(self):
        # Nothing in the client caps the size. Huuray asks for at least 20 MB.
        content = b"%PDF-1.7\n" + bytes(range(256)) * 65_536
        body = envelope(200, [document(5123401, content)])
        assert len(body["Documents"][0]["Content"]) > 21_000_000
        client, calls = make_client(ready(body))
        result = client.pdfs.get(order_uid=ORDER_UID)
        assert len(calls) == 1
        assert result.documents[0].content == content


class TestANotReadyAnswer:
    def test_a_202_is_not_ready_and_carries_retry_after(self):
        client, calls = make_client(not_ready("30"))
        result = client.pdfs.get(order_uid=ORDER_UID)
        assert result == PdfResult(ready=False, order_uid=ORDER_UID, documents=[], retry_after=30)
        assert len(calls) == 1

    def test_retry_after_is_none_without_the_header(self):
        client, _ = make_client(not_ready(None))
        assert client.pdfs.get(order_uid=ORDER_UID).retry_after is None

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
            ("86400", 86400),
        ],
    )
    def test_reads_retry_after_as_whole_seconds_or_none(self, header, seconds):
        client, _ = make_client(not_ready(header))
        assert client.pdfs.get(order_uid=ORDER_UID).retry_after == seconds

    def test_get_asks_once_it_is_get_when_ready_that_waits(self):
        client, calls = make_client(not_ready(), retry=RETRYING)
        assert client.pdfs.get(order_uid=ORDER_UID).ready is False
        assert len(calls) == 1


class TestGarbledContent:
    """A 200 whose documents cannot be read is a transport fault, like a body that is not JSON."""

    @pytest.mark.parametrize(
        "body",
        [
            {"Documents": [{"VoucherIDs": [1], "Content": "LEAKED-CODE-4711"}]},
            {"Documents": [{"VoucherIDs": [1], "Content": "LEAKEDCODE"}]},
            {"Documents": [{"VoucherIDs": [1], "Content": "LEAKEDCODE12\n"}]},
            {"Documents": [{"VoucherIDs": [1], "Content": "LEAKÉDCODE12"}]},
            {"Documents": [document(1, PDF_ONE), {"Content": "LEAKED!!"}]},
            {"Documents": [{"VoucherIDs": [1], "Content": 4711}]},
            {"Documents": [{"VoucherIDs": 5123401, "Content": b64(PDF_ONE)}]},
            {"Documents": "LEAKED"},
            {"Documents": ["LEAKED"]},
            ["LEAKED"],
        ],
        ids=[
            "not-the-alphabet",
            "bad-padding",
            "line-break",
            "non-ascii",
            "second-document",
            "not-a-string",
            "voucher-ids-not-a-list",
            "documents-not-a-list",
            "document-not-an-object",
            "body-not-an-object",
        ],
    )
    def test_raises_a_connection_error_that_never_quotes_the_content(self, body):
        client, calls = make_client(MockResponse(status=200, json=body))
        with pytest.raises(HuurayConnectionError, match="could not be read") as caught:
            client.pdfs.get(order_uid=ORDER_UID)
        assert type(caught.value) is HuurayConnectionError
        assert len(calls) == 1
        for dumped in (str(caught.value), repr(caught.value), repr(caught.value.args)):
            assert "LEAKED" not in dumped
            assert "LEAKÉD" not in dumped
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_says_what_was_wrong_and_how_large_the_body_was(self):
        client, _ = make_client(MockResponse(status=200, text='{"Documents":[{"Content":"x"}]}'))
        with pytest.raises(HuurayConnectionError) as caught:
            client.pdfs.get(order_uid=ORDER_UID)
        assert str(caught.value) == (
            "POST /v4/Pdf returned HTTP 200 but the body could not be read: a document's "
            "Content is not valid base64 (31 bytes)."
        )

    def test_is_retried_like_any_unreadable_answer_to_a_read(self):
        client, calls = make_client(
            [MockResponse(status=200, json={"Documents": [{"Content": "!!"}]}), ready()],
            retry=RETRYING,
        )
        assert client.pdfs.get(order_uid=ORDER_UID) == READY_RESULT
        assert len(calls) == 2
        assert len(set(nonces(calls))) == 2

    def test_a_body_that_is_not_json_is_the_same_fault(self):
        client, _ = make_client(MockResponse(status=200, text="LEAKED <html>"))
        with pytest.raises(HuurayConnectionError, match="not valid JSON") as caught:
            client.pdfs.get(order_uid=ORDER_UID)
        assert "LEAKED" not in str(caught.value)

    async def test_the_async_client_raises_the_same_error(self):
        client, _ = make_async_client(
            MockResponse(status=200, json={"Documents": [{"Content": "LEAKED"}]})
        )
        async with client:
            with pytest.raises(HuurayConnectionError, match="not valid base64") as caught:
                await client.pdfs.get(order_uid=ORDER_UID)
        assert "LEAKED" not in str(caught.value)


class TestRetries:
    """A read, despite being a POST: repeated on a 5xx or a connection failure."""

    def test_retries_a_503_with_a_new_nonce_for_each_attempt(self):
        client, calls = make_client([MockResponse(status=503), ready()], retry=RETRYING)
        assert client.pdfs.get(order_uid=ORDER_UID) == READY_RESULT
        assert len(calls) == 2
        assert len(set(nonces(calls))) == 2
        assert calls[0].body == calls[1].body

    def test_retries_after_a_connection_failure(self):
        client, calls = make_client(
            [MockResponse(raises=httpx.ConnectError("refused")), ready()], retry=RETRYING
        )
        assert client.pdfs.get(order_uid=ORDER_UID).ready is True
        assert len(calls) == 2

    def test_gives_up_with_the_ordinary_server_error_never_an_indeterminate_one(self):
        client, calls = make_client(MockResponse(status=503), retry=RETRYING)
        with pytest.raises(HuurayServerError):
            client.pdfs.get(order_uid=ORDER_UID)
        assert len(calls) == 3

    def test_a_timeout_is_the_ordinary_timeout_error_with_no_note(self):
        client, calls = make_client(
            MockResponse(raises=httpx.ReadTimeout("timed out")), retry=RETRYING, timeout=100.0
        )
        with pytest.raises(HuurayTimeoutError) as caught:
            client.pdfs.get(order_uid=ORDER_UID)
        assert len(calls) == 3
        assert str(caught.value) == "POST /v4/Pdf timed out after 100.0s."

    @pytest.mark.parametrize("status", [400, 401, 404, 422])
    def test_does_not_retry_a_4xx(self, status):
        client, calls = make_client(MockResponse(status=status), retry=RETRYING)
        with pytest.raises(HuurayAPIError):
            client.pdfs.get(order_uid=ORDER_UID)
        assert len(calls) == 1

    async def test_the_async_client_retries_a_503_too(self):
        client, calls = make_async_client([MockResponse(status=503), ready()], retry=RETRYING)
        async with client:
            assert await client.pdfs.get(order_uid=ORDER_UID) == READY_RESULT
        assert len(calls) == 2
        assert len(set(nonces(calls))) == 2


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "message", "expected"),
        [
            (404, "No order was found with the given OrderUID", HuurayNotFoundError),
            (
                404,
                "The voucher was not found on the order, or it is cancelled",
                HuurayNotFoundError,
            ),
            (
                422,
                "The PDF can only be fetched for orders with at most 3 receivers",
                HuurayValidationError,
            ),
            (401, "Restricted Access", HuurayAuthError),
            (400, "OrderUID is required", HuurayAPIError),
            (500, "The PDF could not be generated", HuurayServerError),
        ],
    )
    def test_maps_the_envelope_to_the_existing_error_types(self, status, message, expected):
        client, _ = make_client(MockResponse(status=status, json=envelope(status, [], message)))
        with pytest.raises(expected, match=message) as caught:
            client.pdfs.get(order_uid=ORDER_UID)
        assert type(caught.value) is expected
        assert caught.value.http_status == status
        assert caught.value.status == status
        assert caught.value.status_message == message

    def test_a_problem_details_400_from_the_framework_is_a_plain_api_error(self):
        client, _ = make_client(
            MockResponse(
                status=400,
                json={
                    "title": "One or more validation errors occurred.",
                    "status": 400,
                    "errors": {"VoucherID": ["The JSON value could not be converted."]},
                },
            )
        )
        with pytest.raises(HuurayAPIError) as caught:
            client.pdfs.get(order_uid=ORDER_UID)
        assert type(caught.value) is HuurayAPIError
        assert caught.value.http_status == 400

    def test_an_error_never_carries_a_documents_content(self):
        # Error bodies are the same envelope, with Documents empty. Should one
        # ever carry a document, its content stays out of the error.
        leaked = b64(b"%PDF-1.7 LEAKED-CODE-4711")
        client, _ = make_client(
            MockResponse(
                status=422,
                json=envelope(422, [{"VoucherIDs": [1], "Content": leaked}], "nope"),
            )
        )
        with pytest.raises(HuurayValidationError) as caught:
            client.pdfs.get(order_uid=ORDER_UID)
        error = caught.value
        for dumped in (
            str(error),
            repr(error),
            repr(error.args),
            repr(error.body),
            safe_json(error.body),
        ):
            assert leaked not in dumped
        assert error.body["Documents"][0]["Content"] == BEARER_MARKER

    async def test_the_async_client_maps_errors_the_same_way(self):
        client, _ = make_async_client(
            MockResponse(status=404, json=envelope(404, [], "Order not found"))
        )
        async with client:
            with pytest.raises(HuurayNotFoundError, match="Order not found"):
                await client.pdfs.get(order_uid=ORDER_UID)


class TestGetWhenReady:
    def test_returns_at_once_when_the_first_answer_is_ready(self, clock):
        client, calls = make_client([ready()])
        assert client.pdfs.get_when_ready(order_uid=ORDER_UID) == READY_RESULT
        assert len(calls) == 1
        assert clock.sleeps == []

    def test_waits_retry_after_between_attempts_signing_each_with_a_new_nonce(self, clock):
        client, calls = make_client([not_ready("5"), not_ready("7"), ready()])
        result = client.pdfs.get_when_ready(
            order_uid=ORDER_UID, voucher_id=5123401, pdf_template_uid=TEMPLATE_UID, combine=False
        )
        assert result == READY_RESULT
        assert clock.sleeps == [5, 7]
        assert len(calls) == 3
        assert len(set(nonces(calls))) == 3
        assert len({call.headers["x-api-hash"] for call in calls}) == 3
        assert [call.body for call in calls] == [
            {
                "OrderUID": ORDER_UID,
                "VoucherID": 5123401,
                "PDFTemplateUid": TEMPLATE_UID,
                "Combine": False,
            }
        ] * 3

    @pytest.mark.parametrize("header", [None, "soon", "-5", "Wed, 21 Oct 2026 07:28:00 GMT"])
    def test_waits_30_seconds_when_a_202_has_no_usable_retry_after(self, clock, header):
        client, calls = make_client([not_ready(header), ready()])
        assert client.pdfs.get_when_ready(order_uid=ORDER_UID).ready is True
        assert clock.sleeps == [30]
        assert len(calls) == 2

    def test_a_retry_after_of_zero_still_waits_one_second(self, clock):
        # Never back-to-back requests, whatever a server or proxy sends.
        client, calls = make_client([not_ready("0"), not_ready("00"), ready()])
        assert client.pdfs.get_when_ready(order_uid=ORDER_UID).ready is True
        assert clock.sleeps == [1, 1]
        assert len(calls) == 3

    def test_the_one_second_floor_counts_towards_max_wait(self, clock):
        # The floored wait, not the 0 the API asked for, is what must fit.
        client, calls = make_client([not_ready("0"), not_ready("0")])
        with pytest.raises(HuurayTimeoutError):
            client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=1.5)
        assert clock.sleeps == [1]
        assert len(calls) == 2

    def test_gives_up_before_a_wait_would_pass_max_wait_with_the_last_status(self, clock):
        # 30 + 30 ends exactly at max_wait, so that wait is taken; a third would pass it.
        client, calls = make_client(
            [not_ready("30", "first"), not_ready("30", "second"), not_ready("30", "third")]
        )
        with pytest.raises(HuurayTimeoutError) as caught:
            client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=60)
        assert type(caught.value) is HuurayTimeoutError
        assert caught.value.timeout == 60
        assert str(caught.value) == (
            "POST /v4/Pdf timed out after 60s. The gift card PDF was still not ready, and "
            "waiting 30s more would pass max_wait. Last status: third"
        )
        assert clock.sleeps == [30, 30]
        assert len(calls) == 3

    def test_gives_up_at_once_when_the_first_wait_would_pass_max_wait(self, clock):
        client, calls = make_client([not_ready("30")])
        with pytest.raises(HuurayTimeoutError, match="Last status: " + STILL_PROCESSING):
            client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=10)
        assert clock.sleeps == []
        assert len(calls) == 1

    def test_gives_up_on_the_30_second_default_too(self, clock):
        client, calls = make_client([not_ready(None)])
        with pytest.raises(HuurayTimeoutError, match="waiting 30s more"):
            client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=29.5)
        assert len(calls) == 1

    def test_the_error_leaves_out_a_status_the_api_did_not_give(self, clock):
        client, _ = make_client([MockResponse(status=202, json={}, headers={"Retry-After": "30"})])
        with pytest.raises(HuurayTimeoutError) as caught:
            client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=0)
        assert str(caught.value).endswith("waiting 30s more would pass max_wait.")

    def test_max_wait_defaults_to_ten_minutes(self, clock):
        client, calls = make_client([not_ready("300")] * 3)
        with pytest.raises(HuurayTimeoutError) as caught:
            client.pdfs.get_when_ready(order_uid=ORDER_UID)
        assert caught.value.timeout == 600
        assert clock.sleeps == [300, 300]
        assert len(calls) == 3

    def test_the_time_each_call_takes_counts_towards_max_wait(self, clock):
        # Calls end at 20, 70 and 120 seconds. Counting only the waits, a fourth
        # call would have been made.
        client, calls = client_on_clock(clock, [not_ready("30")] * 3, seconds_per_call=20)
        with pytest.raises(HuurayTimeoutError):
            client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=100)
        assert clock.sleeps == [30, 30]
        assert len(calls) == 3

    def test_an_error_is_raised_at_once_not_waited_out(self, clock):
        client, calls = make_client(
            [not_ready("5"), MockResponse(status=404, json=envelope(404, [], "Order cancelled"))]
        )
        with pytest.raises(HuurayNotFoundError, match="Order cancelled"):
            client.pdfs.get_when_ready(order_uid=ORDER_UID)
        assert clock.sleeps == [5]
        assert len(calls) == 2

    def test_a_503_while_waiting_is_retried_by_the_read_policy(self, clock):
        client, calls = make_client(
            [not_ready("1"), MockResponse(status=503), ready()], retry=RETRYING
        )
        assert client.pdfs.get_when_ready(order_uid=ORDER_UID) == READY_RESULT
        assert clock.sleeps == [1]
        assert len(calls) == 3
        assert len(set(nonces(calls))) == 3

    @pytest.mark.parametrize(
        "max_wait", [-1, -0.001, float("nan"), float("inf"), True, "600", None]
    )
    def test_rejects_a_max_wait_that_could_not_be_honoured_before_sending(self, clock, max_wait):
        client, calls = make_client([ready()])
        with pytest.raises(ValueError, match="max_wait must be a finite number of seconds"):
            client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=max_wait)
        assert calls == []


class TestAsyncGetWhenReady:
    def test_waits_with_asyncio_sleep_and_the_monotonic_clock_by_default(self):
        assert pdfs_module._async_sleep is asyncio.sleep
        assert pdfs_module._sleep is time.sleep
        assert pdfs_module._monotonic is time.monotonic

    async def test_awaits_the_real_asyncio_sleep_without_blocking(self, monkeypatch):
        # Retry-After: 0 is floored to a real one-second wait, the shortest there is.
        monkeypatch.setattr("huuray.resources.pdfs._sleep", blocking_sleep)
        client, calls = make_async_client([not_ready("0"), ready()])
        started = time.monotonic()
        async with client:
            assert await client.pdfs.get_when_ready(order_uid=ORDER_UID) == READY_RESULT
        assert time.monotonic() - started >= 0.9
        assert len(calls) == 2

    async def test_waits_retry_after_between_attempts_signing_each_with_a_new_nonce(
        self, async_clock
    ):
        client, calls = make_async_client([not_ready("5"), not_ready(None), ready()])
        async with client:
            result = await client.pdfs.get_when_ready(order_uid=ORDER_UID, combine=True)
        assert result == READY_RESULT
        assert async_clock.sleeps == [5, 30]
        assert len(calls) == 3
        assert len(set(nonces(calls))) == 3

    async def test_a_retry_after_of_zero_still_waits_one_second(self, async_clock):
        client, calls = make_async_client([not_ready("0"), ready()])
        async with client:
            assert await client.pdfs.get_when_ready(order_uid=ORDER_UID) == READY_RESULT
        assert async_clock.sleeps == [1]
        assert len(calls) == 2

    async def test_gives_up_before_a_wait_would_pass_max_wait(self, async_clock):
        client, calls = make_async_client([not_ready("30", "first"), not_ready("30", "second")])
        async with client:
            with pytest.raises(HuurayTimeoutError, match="Last status: second") as caught:
                await client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=45)
        assert caught.value.timeout == 45
        assert async_clock.sleeps == [30]
        assert len(calls) == 2

    async def test_rejects_a_bad_max_wait_before_sending(self, async_clock):
        client, calls = make_async_client([ready()])
        async with client:
            with pytest.raises(ValueError, match="max_wait"):
                await client.pdfs.get_when_ready(order_uid=ORDER_UID, max_wait=float("nan"))
        assert calls == []

    async def test_maps_a_202_like_the_sync_client(self):
        client, _ = make_async_client(not_ready("12"))
        async with client:
            result = await client.pdfs.get(order_uid=ORDER_UID)
        assert result == PdfResult(ready=False, order_uid=ORDER_UID, documents=[], retry_after=12)


class TestNothingDumpsTheContent:
    """The PDF shows the redeemable code: every dump path shows its size, never the bytes."""

    CONTENT = b"%PDF-1.7 LEAKED-CODE-4711"
    DOCUMENT = PdfDocument(
        voucher_ids=[5123401],
        pdf_template_uid=TEMPLATE_UID,
        file_name="giftcard-5123401.pdf",
        content_type="application/pdf",
        content=CONTENT,
    )
    RESULT = PdfResult(ready=True, order_uid=ORDER_UID, documents=[DOCUMENT])

    def test_repr_and_str_show_the_size_never_the_bytes(self):
        for dumped in (
            repr(self.DOCUMENT),
            str(self.DOCUMENT),
            repr(self.RESULT),
            str(self.RESULT),
            f"{self.RESULT}",
            f"{[self.RESULT]!r}",
        ):
            assert "LEAKED" not in dumped
            assert "content=[25 bytes]" in dumped
        assert repr(self.DOCUMENT) == (
            f"PdfDocument(voucher_ids=[5123401], pdf_template_uid='{TEMPLATE_UID}', "
            "file_name='giftcard-5123401.pdf', content_type='application/pdf', "
            "content=[25 bytes])"
        )

    def test_repr_of_an_absent_content_is_plain_none(self):
        document = PdfDocument(
            voucher_ids=[], pdf_template_uid=None, file_name=None, content_type=None, content=None
        )
        assert repr(document).endswith("content=None)")

    def test_pprint_shows_the_size_never_the_bytes(self):
        # A narrow width makes pprint lay the dataclass out field by field.
        for width in (20, 80):
            dumped = pprint.pformat(self.RESULT, width=width)
            assert "LEAKED" not in dumped
            assert "[25 bytes]" in dumped

    def test_redact_and_safe_json_show_the_size_never_the_bytes(self):
        assert redact(self.RESULT)["documents"][0]["content"] == "[25 bytes]"
        assert redact(self.DOCUMENT)["content"] == "[25 bytes]"
        dumped = safe_json(self.RESULT)
        assert "LEAKED" not in dumped
        assert "[25 bytes]" in dumped

    def test_logging_a_result_carries_no_content(self, caplog):
        logger = logging.getLogger("huuray-test")
        with caplog.at_level(logging.INFO, logger="huuray-test"):
            logger.info("fetched %s", self.RESULT)
            logger.info("document %r", self.DOCUMENT)
        assert "LEAKED" not in caplog.text
        assert "[25 bytes]" in caplog.text

    def test_the_raw_response_the_resource_maps_carries_no_content(self):
        body = envelope(200, [document(5123401, self.CONTENT)])
        client, _ = make_client(MockResponse(status=200, json=body, headers={"Retry-After": "1"}))
        op = pdfs_module._operation(ORDER_UID, None, None, None)
        raw = client._send(op)
        assert "LEAKED" not in repr(raw)
        assert b64(self.CONTENT) not in repr(raw)
        # httpx stores header names in lower case, so the check ignores case.
        assert "retry-after" not in repr(raw).lower()
        assert raw.headers["retry-after"] == "1"
