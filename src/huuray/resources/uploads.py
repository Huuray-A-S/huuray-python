"""``POST /v4/Upload`` — a purchase order file, staged for a later order."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, BinaryIO, Optional, Union

from ..auth import _is_sendable_header_value
from ..redact import _mask_partial
from ._base import AsyncResource, FilePart, Operation, Resource

#: The part's content type when the caller names none.
_DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: An unpaired UTF-16 surrogate, which UTF-8 cannot encode.
_SURROGATE = re.compile("[\ud800-\udfff]")

#: Added to a timeout, connection or unreadable-response error from an upload.
_MAY_HAVE_BEEN_STORED = (
    "The upload may still have been stored, and may hold a pending upload slot until it is "
    "used or cleaned up. Uploads are never retried automatically: each one stages a new file."
)


@dataclass(frozen=True, repr=False)
class UploadResult:
    """A staged file. Pass ``token`` as ``purchase_order_file_token`` on an order.

    ``file_name`` is masked in ``repr()``: a file name can carry personal data.
    """

    #: The token identifying the uploaded file. The order it is used with
    #: consumes it.
    token: Optional[str]
    #: The file name the upload was stored with.
    file_name: Optional[str]
    #: The content type the file was recognized as.
    content_type: Optional[str]
    #: The size of the uploaded file in bytes.
    size: Optional[int]

    def __repr__(self) -> str:
        name = repr(_mask_partial(self.file_name)) if self.file_name else repr(self.file_name)
        return (
            f"UploadResult(token={self.token!r}, file_name={name}, "
            f"content_type={self.content_type!r}, size={self.size!r})"
        )


def _read(file: Union[bytes, BinaryIO]) -> bytes:
    """The file's content, read once and in full before anything is sent."""
    if isinstance(file, (bytes, bytearray, memoryview)):
        return bytes(file)
    read = getattr(file, "read", None)
    content = read() if callable(read) else None
    if not isinstance(content, bytes):
        # A str is most likely a path. Sent as it is, the path would be uploaded
        # as the file's content.
        raise ValueError(
            "file must be bytes or a file object opened in binary mode, e.g. "
            "open(path, 'rb'). Pass the file's content, not its path."
        )
    return content


def _operation(
    file: Union[bytes, BinaryIO], file_name: str, content_type: Optional[str]
) -> Operation:
    # Neither value is quoted: a file name can carry personal data.
    if not isinstance(file_name, str) or not file_name:
        raise ValueError("file_name is required: the file is sent as a part with a file name.")
    # Checked rather than caught: the UnicodeEncodeError would carry the name.
    if _SURROGATE.search(file_name):
        raise ValueError(
            "file_name cannot be UTF-8 encoded: it holds an unpaired surrogate, e.g. an "
            "undecodable byte read from a path."
        )
    # Written into the part's own headers, where a line break would add a header
    # or end the part.
    if content_type is not None and not _is_sendable_header_value(content_type):
        raise ValueError(
            "content_type cannot be sent as the part's Content-Type header: it is empty, or "
            "holds a line break, tab, NUL or other control character, a non-ASCII character, "
            "or a space at either end."
        )
    part = FilePart(
        name="File",
        file_name=file_name,
        content=_read(file),
        content_type=_DEFAULT_CONTENT_TYPE if content_type is None else content_type,
    )
    # retryable is False and must stay False: every upload stages a new file,
    # holding a pending upload slot, and no endpoint lists them.
    return Operation(
        method="POST",
        path="/v4/Upload",
        files=(part,),
        retryable=False,
        unknown_outcome_note=_MAY_HAVE_BEEN_STORED,
    )


def _map(data: Any) -> UploadResult:
    payload = data or {}
    return UploadResult(
        token=payload.get("Token"),
        file_name=payload.get("FileName"),
        content_type=payload.get("ContentType"),
        size=payload.get("Size"),
    )


class UploadsResource(Resource):
    def create(
        self,
        *,
        file: Union[bytes, BinaryIO],
        file_name: str,
        content_type: Optional[str] = None,
    ) -> UploadResult:
        """Upload a purchase order file, for an order to attach to its invoice.

        ``POST /v4/Upload`` — ``multipart/form-data``, the file sent as the part
        ``File``.

        Pass the returned ``token`` as ``purchase_order_file_token`` to
        ``orders.create()``, ``orders.create_sync()`` or ``send_reward()``. The
        order consumes it.

        **Never retried.** Each upload stages a new file, holding one of your
        account's pending upload slots until an order uses it, and there is no
        call to find an upload whose answer was lost. A timeout or a dropped
        connection raises :class:`~huuray.HuurayTimeoutError` or
        :class:`~huuray.HuurayConnectionError`, saying the upload may still have
        been stored.

        :param file: The content, as ``bytes`` or a file object opened in binary
            mode. It is read in full before the request is sent.
        :param file_name: The file's name, sent with it.
        :param content_type: The file's type, e.g. ``application/pdf``. Omit to
            send ``application/octet-stream``.
        """
        op = _operation(file, file_name, content_type)
        return _map(self._client._send(op).data)


class AsyncUploadsResource(AsyncResource):
    async def create(
        self,
        *,
        file: Union[bytes, BinaryIO],
        file_name: str,
        content_type: Optional[str] = None,
    ) -> UploadResult:
        """Upload a purchase order file. See :meth:`UploadsResource.create`.

        ``POST /v4/Upload``
        """
        op = _operation(file, file_name, content_type)
        return _map((await self._client._send(op)).data)
