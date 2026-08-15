"""A fake HTTP transport, and clients wired to it.

No test in this suite touches the network: ordering gift cards from a test
runner would spend real money. Everything goes through :class:`RecordingTransport`,
which records what the SDK sent and replays canned responses.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import httpx

from huuray import AsyncHuurayClient, HuurayClient, RetryOptions


@dataclass
class CapturedRequest:
    """One request the SDK made."""

    method: str
    #: Full URL as requested, e.g. ``https://api.huuray.com/v4/Balance?x=1``.
    url: str
    #: Scheme and host only, e.g. ``https://api.huuray.com`` — for pinning the base URL.
    origin: str
    #: Path only, e.g. ``/v4/Order``.
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    #: Parsed JSON body, or ``None`` when no body was sent.
    body: Any = None
    #: ``True`` when no body was sent at all — distinct from an empty object.
    body_omitted: bool = True


@dataclass
class MockResponse:
    """One canned reply."""

    status: int = 200
    #: JSON body. Ignored when ``text`` is set.
    json: Any = None
    #: Raw body text; takes precedence over ``json``. Use for garbled responses.
    text: Optional[str] = None
    #: Raise instead of responding, to simulate a failure before headers arrive.
    raises: Optional[Exception] = None
    #: Resolve the response, but make reading its body raise — a mid-body drop.
    #:
    #: This is the case that matters most: an HTTP library resolves on headers,
    #: and a fault while the body streams must not escape the error taxonomy.
    body_raises: Optional[Exception] = None


class _ExplodingStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A response body that fails partway through, in both sync and async."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __iter__(self) -> Iterator[bytes]:
        raise self._error

    async def __aiter__(self) -> Any:
        raise self._error
        # Unreachable on purpose: the yield is what makes this an async
        # generator, which is what httpx requires of an async response stream.
        yield b""  # type: ignore[unreachable]  # pragma: no cover


@dataclass
class RecordingTransport:
    """Records requests and replays queued responses.

    Queue semantics: a list is strict — one response per request, and a request
    beyond the end raises, so a test can never silently absorb an extra HTTP
    call (an accidental order retry is exactly the bug class this suite exists
    to catch). A single response repeats for every request.
    """

    responses: Union[MockResponse, list[MockResponse], None] = None
    calls: list[CapturedRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._strict: bool = isinstance(self.responses, list)
        self._queue: list[MockResponse]
        if self.responses is None:
            self._queue = [MockResponse()]
        elif isinstance(self.responses, list):
            self._queue = list(self.responses)
        else:
            self._queue = [self.responses]

    # ---------------------------------------------------------------- record

    def _record(self, request: httpx.Request) -> MockResponse:
        raw = request.content
        self.calls.append(
            CapturedRequest(
                method=request.method,
                url=str(request.url),
                origin=f"{request.url.scheme}://{request.url.netloc.decode('ascii')}",
                path=request.url.path,
                query=dict(request.url.params),
                headers=dict(request.headers.items()),
                body=json.loads(raw) if raw else None,
                body_omitted=not raw,
            )
        )

        if self._strict:
            if not self._queue:
                raise AssertionError(
                    f"RecordingTransport: request #{len(self.calls)} "
                    f"({request.method} {request.url.path}) exceeds the queued responses — "
                    "the code under test made more HTTP calls than the test expected."
                )
            return self._queue.pop(0)
        return self._queue[0]

    def _build(self, mock: MockResponse) -> httpx.Response:
        if mock.raises is not None:
            raise mock.raises

        headers = {"Content-Type": "application/json"}
        if mock.body_raises is not None:
            return httpx.Response(
                mock.status, headers=headers, stream=_ExplodingStream(mock.body_raises)
            )

        body = (
            mock.text if mock.text is not None else json.dumps(mock.json or {"Status": mock.status})
        )
        return httpx.Response(mock.status, headers=headers, content=body)

    # -------------------------------------------------------------- transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._build(self._record(request))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._build(self._record(request))


def make_client(
    responses: Union[MockResponse, list[MockResponse], None] = None,
    **options: Any,
) -> tuple[HuurayClient, list[CapturedRequest]]:
    """A synchronous client wired to a recording transport, with throwaway credentials.

    Named ``make_client`` and not ``test_client`` on purpose: every module that
    imports it re-exports the name, and pytest collects anything called
    ``test_*`` at module scope. Under the old name it was collected as a test in
    four files, where it passed vacuously and inflated the count.
    """
    transport = RecordingTransport(responses)
    options.setdefault("retry", RetryOptions(max_retries=0))
    client = HuurayClient(
        api_token="test-token",
        api_secret="test-secret",
        transport=httpx.MockTransport(transport.handle_request),
        **options,
    )
    return client, transport.calls


def make_async_client(
    responses: Union[MockResponse, list[MockResponse], None] = None,
    **options: Any,
) -> tuple[AsyncHuurayClient, list[CapturedRequest]]:
    """An asynchronous client wired to the same recording transport."""
    transport = RecordingTransport(responses)
    options.setdefault("retry", RetryOptions(max_retries=0))
    client = AsyncHuurayClient(
        api_token="test-token",
        api_secret="test-secret",
        transport=httpx.MockTransport(transport.handle_async_request),
        **options,
    )
    return client, transport.calls
