"""Purchase order uploads: the multipart request, the result, and why it is never retried."""

from __future__ import annotations

import io
import re
from typing import Any

import httpx
import pytest

from huuray import (
    HuurayAPIError,
    HuurayConnectionError,
    HuurayServerError,
    HuurayTimeoutError,
    HuurayValidationError,
    RetryOptions,
    UploadResult,
    redact,
    safe_json,
)
from huuray.auth import sign_request
from huuray.resources._base import FilePart
from huuray.resources.uploads import _operation

from .helpers import CapturedPart, MockResponse, make_async_client, make_client

#: Every byte value, plus the sequences that frame a multipart body.
AWKWARD_BYTES = bytes(range(256)) + b"\r\n--\r\n\r\n--boundary--\r\n" + bytes(range(255, -1, -1))

UPLOADED: dict[str, Any] = {
    "Token": "60050460-7a2d-42a8-a4dd-5cef88ad8374",
    "FileName": "purchase-order-4711.pdf",
    "ContentType": "application/pdf",
    "Size": 48213,
    "Status": 201,
    "StatusMessage": "OK",
}

#: What the timeout, connection and unreadable-response errors of an upload add.
MAY_HAVE_BEEN_STORED = "The upload may still have been stored"


def upload(client: Any, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "file": b"%PDF-1.7",
        "file_name": "purchase-order-4711.pdf",
        "content_type": "application/pdf",
        **overrides,
    }
    return client.uploads.create(**arguments)


class TestTheRequest:
    def test_is_one_signed_multipart_post_to_v4_upload(self):
        client, calls = make_client(
            MockResponse(status=201, json=UPLOADED), nonce_factory=lambda: "fixed-nonce"
        )
        upload(client)

        assert len(calls) == 1
        call = calls[0]
        assert (call.method, call.origin, call.path, call.query) == (
            "POST",
            "https://api.huuray.com",
            "/v4/Upload",
            {},
        )
        assert call.headers["x-api-token"] == "test-token"
        assert call.headers["x-api-nonce"] == "fixed-nonce"
        assert call.headers["x-api-hash"] == sign_request("test-secret", "fixed-nonce")
        assert call.headers["accept"] == "application/json"
        # httpx writes the boundary; the body must be framed by that same one.
        assert re.fullmatch(
            r"multipart/form-data; boundary=[0-9a-f]{32}", call.headers["content-type"]
        )
        assert call.parse_error is None
        assert call.headers["content-length"] == str(len(call.content))

    def test_sends_exactly_one_part_named_file_with_its_filename_and_type(self):
        client, calls = make_client(MockResponse(status=201, json=UPLOADED))
        upload(client)
        assert calls[0].parts == [
            CapturedPart(
                name="File",
                filename="purchase-order-4711.pdf",
                content_type="application/pdf",
                data=b"%PDF-1.7",
            )
        ]
        assert calls[0].body is None

    def test_sends_every_byte_intact(self):
        client, calls = make_client(MockResponse(status=201, json=UPLOADED))
        upload(client, file=AWKWARD_BYTES)
        assert [part.data for part in calls[0].parts or []] == [AWKWARD_BYTES]

    def test_sends_application_octet_stream_when_no_content_type_is_given(self):
        client, calls = make_client(MockResponse(status=201, json=UPLOADED))
        client.uploads.create(file=b"%PDF-1.7", file_name="purchase-order-4711.pdf")
        # Not guessed from the file name: the platform's type table would decide.
        assert [part.content_type for part in calls[0].parts or []] == ["application/octet-stream"]

    @pytest.mark.parametrize(
        "make_file",
        [
            lambda: io.BytesIO(AWKWARD_BYTES),
            lambda: bytearray(AWKWARD_BYTES),
            lambda: memoryview(AWKWARD_BYTES),
        ],
        ids=["binary-file-object", "bytearray", "memoryview"],
    )
    def test_accepts_a_binary_file_object_or_any_bytes_like_value(self, make_file):
        client, calls = make_client(MockResponse(status=201, json=UPLOADED))
        upload(client, file=make_file())
        assert [part.data for part in calls[0].parts or []] == [AWKWARD_BYTES]

    def test_sends_the_file_name_as_given_non_ascii_included(self):
        client, calls = make_client(MockResponse(status=201, json=UPLOADED))
        upload(client, file_name="indkøbsordre 4711.pdf")
        assert [part.filename for part in calls[0].parts or []] == ["indkøbsordre 4711.pdf"]

    @pytest.mark.parametrize(
        ("file", "file_name", "content_type"),
        [
            (b"", "empty.pdf", "application/pdf"),
            (b"<svg/>", "drawing.svg", "image/svg+xml"),
            (b"x" * 10_000_001, "large.pdf", "application/pdf"),
            (b"MZ", "no-extension", "application/x-msdownload"),
        ],
        ids=["empty", "svg", "over-10-mb", "any-type"],
    )
    def test_leaves_size_and_type_to_the_api(self, file, file_name, content_type):
        client, calls = make_client(MockResponse(status=201, json=UPLOADED))
        client.uploads.create(file=file, file_name=file_name, content_type=content_type)
        assert len(calls) == 1
        assert [part.data for part in calls[0].parts or []] == [file]


class TestInputGuards:
    """Programming mistakes, refused before anything is sent. None quotes the value."""

    @pytest.mark.parametrize(
        "file",
        ["C:/orders/leaky-jane-doe.pdf", io.StringIO("leaky text"), None, 42],
        ids=["a-path", "a-text-file", "none", "an-int"],
    )
    def test_rejects_a_file_that_is_not_bytes_or_a_binary_file_object(self, file):
        client, calls = make_client()
        with pytest.raises(ValueError, match="bytes or a file object opened in binary") as caught:
            upload(client, file=file)
        assert calls == []
        assert "leaky" not in str(caught.value)

    @pytest.mark.parametrize("file_name", ["", None])
    def test_requires_a_file_name(self, file_name):
        client, calls = make_client()
        with pytest.raises(ValueError, match="file_name is required"):
            upload(client, file_name=file_name)
        assert calls == []

    def test_rejects_a_file_name_that_cannot_be_utf8_encoded_without_quoting_it(self):
        client, calls = make_client()
        with pytest.raises(ValueError, match="unpaired surrogate") as caught:
            upload(client, file_name="leaky-jane\udcff.pdf")
        assert calls == []
        assert "leaky" not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    @pytest.mark.parametrize(
        "content_type",
        [
            "application/pdf\r\nX-Leaky: yes",
            "application/pdf\n",
            "application/leaky\x00",
            "",
            " application/pdf",
            "application/pdf ",
            "application/pdf-leaky-ä",
        ],
    )
    def test_rejects_a_content_type_that_cannot_be_sent_as_a_part_header(self, content_type):
        # Written into the part's headers, a line break would add a header or end
        # the part early.
        client, calls = make_client()
        with pytest.raises(ValueError, match="part's Content-Type header") as caught:
            upload(client, content_type=content_type)
        assert calls == []
        assert "leaky" not in str(caught.value).lower()

    async def test_the_async_client_applies_the_same_guards(self):
        client, calls = make_async_client()
        async with client:
            with pytest.raises(ValueError, match="bytes or a file object"):
                await upload(client, file="C:/orders/po.pdf")
            with pytest.raises(ValueError, match="file_name is required"):
                await upload(client, file_name="")
            with pytest.raises(ValueError, match="part's Content-Type header"):
                await upload(client, content_type="application/pdf\r\nX: y")
        assert calls == []


class TestTheResult:
    def test_a_201_is_success_and_maps_every_field(self):
        client, _ = make_client(MockResponse(status=201, json=UPLOADED))
        assert upload(client) == UploadResult(
            token="60050460-7a2d-42a8-a4dd-5cef88ad8374",
            file_name="purchase-order-4711.pdf",
            content_type="application/pdf",
            size=48213,
        )

    def test_maps_absent_or_null_fields_to_none(self):
        client, _ = make_client(
            MockResponse(status=201, json={"Token": "t", "ContentType": None, "Status": 201})
        )
        assert upload(client) == UploadResult(
            token="t", file_name=None, content_type=None, size=None
        )

    def test_repr_masks_the_file_name_which_can_carry_personal_data(self):
        client, _ = make_client(
            MockResponse(status=201, json={**UPLOADED, "FileName": "jane-doe-order.pdf"})
        )
        printed = repr(upload(client))
        assert "jane-doe" not in printed
        assert "file_name='ja***df'" in printed
        assert "60050460-7a2d-42a8-a4dd-5cef88ad8374" in printed
        assert "size=48213" in printed

    def test_repr_of_an_absent_file_name_is_plain_none(self):
        result = UploadResult(token="t", file_name=None, content_type=None, size=None)
        assert "file_name=None" in repr(result)


class TestNeverRetried:
    """An upload stages a new file on every call, so a retry would stage a second one."""

    RETRYING = RetryOptions(max_retries=3, base_delay=0.001)

    def test_makes_one_attempt_on_a_503(self):
        client, calls = make_client(MockResponse(status=503), retry=self.RETRYING)
        with pytest.raises(HuurayServerError):
            upload(client)
        assert len(calls) == 1

    def test_makes_one_attempt_on_a_connection_failure_and_says_it_may_be_stored(self):
        client, calls = make_client(
            MockResponse(raises=httpx.ConnectError("socket hang up")), retry=self.RETRYING
        )
        with pytest.raises(HuurayConnectionError) as caught:
            upload(client)
        assert len(calls) == 1
        assert type(caught.value) is HuurayConnectionError
        assert "socket hang up" in str(caught.value)
        assert MAY_HAVE_BEEN_STORED in str(caught.value)
        assert "pending upload slot" in str(caught.value)

    @pytest.mark.parametrize(
        "mock",
        [
            MockResponse(raises=httpx.ConnectTimeout("timed out")),
            MockResponse(raises=httpx.WriteTimeout("timed out")),
            MockResponse(body_raises=httpx.ReadTimeout("timed out")),
        ],
        ids=["connect", "write", "mid-body"],
    )
    def test_makes_one_attempt_on_a_timeout_and_says_it_may_be_stored(self, mock):
        client, calls = make_client(mock, retry=self.RETRYING, timeout=12.5)
        with pytest.raises(HuurayTimeoutError) as caught:
            upload(client)
        assert len(calls) == 1
        assert caught.value.timeout == 12.5
        assert str(caught.value) == (
            "POST /v4/Upload timed out after 12.5s. The upload may still have been stored, "
            "and may hold a pending upload slot until it is used or cleaned up. Uploads are "
            "never retried automatically: each one stages a new file."
        )

    def test_an_unreadable_2xx_body_says_it_may_be_stored_without_quoting_the_body(self):
        client, calls = make_client(
            MockResponse(status=201, text="LEAKED <not json>"), retry=self.RETRYING
        )
        with pytest.raises(HuurayConnectionError) as caught:
            upload(client)
        assert len(calls) == 1
        assert MAY_HAVE_BEEN_STORED in str(caught.value)
        assert "LEAKED" not in str(caught.value)

    def test_other_operations_keep_their_messages(self):
        client, _ = make_client(MockResponse(raises=httpx.ConnectTimeout("timed out")))
        with pytest.raises(HuurayTimeoutError) as caught:
            client.balances.list()
        assert str(caught.value) == "GET /v4/Balance timed out after 30.0s."


class TestErrorMapping:
    def test_a_413_is_a_plain_api_error(self):
        # The size limit is enforced by the host before the endpoint runs, so no
        # dedicated class: a 413 flows through the generic mapping.
        client, calls = make_client(MockResponse(status=413, text=""))
        with pytest.raises(HuurayAPIError) as caught:
            upload(client)
        assert type(caught.value) is HuurayAPIError
        assert caught.value.http_status == 413
        assert len(calls) == 1

    def test_a_422_is_a_validation_error_with_its_status_message(self):
        client, _ = make_client(
            MockResponse(
                status=422,
                json={"Status": 422, "StatusMessage": "The file type is not supported"},
            )
        )
        with pytest.raises(HuurayValidationError, match="The file type is not supported"):
            upload(client)

    def test_the_file_name_in_a_retained_error_body_is_masked(self):
        client, _ = make_client(
            MockResponse(
                status=400,
                json={"Status": 400, "StatusMessage": "bad", "FileName": "jane-doe-order.pdf"},
            )
        )
        with pytest.raises(HuurayAPIError) as caught:
            upload(client)
        assert "jane-doe" not in safe_json(caught.value.body)


class TestNothingDumpsTheFile:
    """Every dump path shows the size instead of the bytes, and masks the file name."""

    def test_the_operation_repr_shows_the_size_and_masks_the_file_name(self):
        printed = repr(_operation(b"SECRET-CONTENT", "jane-doe-order.pdf", None))
        assert "SECRET-CONTENT" not in printed
        assert "jane-doe" not in printed
        assert "content=[14 bytes]" in printed
        assert "file_name='ja***df'" in printed

    def test_a_file_part_repr_does_the_same(self):
        part = FilePart(
            name="File",
            file_name="jane-doe-order.pdf",
            content=b"SECRET-CONTENT",
            content_type="application/pdf",
        )
        assert repr(part) == (
            "FilePart(name='File', file_name='ja***df', content=[14 bytes], "
            "content_type='application/pdf')"
        )

    def test_redact_and_safe_json_replace_the_bytes_and_mask_the_file_name(self):
        op = _operation(b"SECRET-CONTENT", "jane-doe-order.pdf", "application/pdf")
        for dumped in (repr(redact(op)), safe_json(op)):
            assert "SECRET-CONTENT" not in dumped
            assert "jane-doe" not in dumped
            assert "[14 bytes]" in dumped

    def test_redact_and_safe_json_mask_the_file_name_on_a_result(self):
        result = UploadResult(
            token="t", file_name="jane-doe-order.pdf", content_type="application/pdf", size=1
        )
        assert redact(result)["file_name"] == "ja***df"
        assert "jane-doe" not in safe_json(result)


class TestAsyncUploads:
    async def test_sends_the_same_request_as_the_sync_client_but_for_the_boundary(self):
        sync_client, sync_calls = make_client(MockResponse(status=201, json=UPLOADED))
        upload(sync_client, file=AWKWARD_BYTES)
        async_client, async_calls = make_async_client(MockResponse(status=201, json=UPLOADED))
        async with async_client:
            await upload(async_client, file=AWKWARD_BYTES)

        def shape(call: Any) -> tuple[Any, ...]:
            return (call.method, call.path, call.media_type, call.content_without_boundary())

        assert [shape(c) for c in async_calls] == [shape(c) for c in sync_calls]
        assert async_calls[0].parts == sync_calls[0].parts

    async def test_maps_a_201(self):
        client, _ = make_async_client(MockResponse(status=201, json=UPLOADED))
        async with client:
            result = await upload(client)
        assert result.token == "60050460-7a2d-42a8-a4dd-5cef88ad8374"
        assert result.size == 48213

    async def test_makes_one_attempt_on_a_503(self):
        client, calls = make_async_client(
            MockResponse(status=503), retry=RetryOptions(max_retries=3, base_delay=0.001)
        )
        async with client:
            with pytest.raises(HuurayServerError):
                await upload(client)
        assert len(calls) == 1

    async def test_makes_one_attempt_on_a_timeout_and_says_it_may_be_stored(self):
        client, calls = make_async_client(
            MockResponse(body_raises=httpx.ReadTimeout("timed out")),
            retry=RetryOptions(max_retries=3, base_delay=0.001),
        )
        async with client:
            with pytest.raises(HuurayTimeoutError, match=MAY_HAVE_BEEN_STORED):
                await upload(client)
        assert len(calls) == 1

    async def test_makes_one_attempt_on_a_connection_failure(self):
        client, calls = make_async_client(
            MockResponse(raises=httpx.ReadError("connection reset")),
            retry=RetryOptions(max_retries=3, base_delay=0.001),
        )
        async with client:
            with pytest.raises(HuurayConnectionError, match=MAY_HAVE_BEEN_STORED):
                await upload(client)
        assert len(calls) == 1
