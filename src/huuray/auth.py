"""Request signing.

Every v4 request carries three headers::

    X-API-TOKEN   your API token
    X-API-NONCE   a random value, single-use within 60 days, max 50 characters
    X-API-HASH    SHA-512 of ( API-SECRET + NONCE )

The specification states the construction but not the encoding of the digest,
so the encoding is configurable and its default is documented below.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Literal

#: How the SHA-512 digest is encoded into the ``X-API-HASH`` header.
HashEncoding = Literal["hex", "hex-upper", "base64", "base64url"]

#: Default digest encoding: lowercase hex.
#:
#: The v4 specification describes ``X-API-HASH`` as "the SHA512 hash of a
#: concatenated string containing ( API-SECRET + NONCE )" without stating an
#: encoding. Lowercase hex was confirmed against the live API on 2026-08-15
#: (``GET /v4/Balance``); the other three candidate encodings returned 401.
#:
#: If you get a 401 with credentials you know are correct, the encoding is the
#: first thing to try: pass ``hash_encoding`` to the client.
DEFAULT_HASH_ENCODING: HashEncoding = "hex"

#: The specification's stated maximum length of ``X-API-NONCE``.
#:
#: Exceeding it is rejected by the API, and a too-long nonce is an easy mistake:
#: 32 random bytes encoded as hex is 64 characters, silently over the limit.
NONCE_MAX_LENGTH = 50

#: Bytes of entropy per generated nonce. 24 bytes -> 32 base64url characters.
_NONCE_BYTES = 24


def generate_nonce() -> str:
    """Generate a nonce: 24 crypto-random bytes as base64url, 32 characters.

    The API stores nonces for 60 days and rejects a repeat, so the only thing
    that matters is that values never collide. 192 bits of entropy makes that
    negligible at any realistic volume, and 32 characters leaves headroom under
    the 50-character cap.

    Avoid timestamps: at second resolution they collide under concurrency, and
    the resulting 401s are intermittent and hard to trace.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(_NONCE_BYTES)).decode("ascii").rstrip("=")


def _encode_digest(digest: bytes, encoding: HashEncoding) -> str:
    if encoding == "hex":
        return digest.hex()
    if encoding == "hex-upper":
        return digest.hex().upper()
    if encoding == "base64":
        return base64.b64encode(digest).decode("ascii")
    if encoding == "base64url":
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    raise ValueError(
        f"Unknown hash encoding {encoding!r}. "
        "Use one of: 'hex', 'hex-upper', 'base64', 'base64url'."
    )


def sign_request(
    api_secret: str,
    nonce: str,
    encoding: HashEncoding = DEFAULT_HASH_ENCODING,
) -> str:
    """Compute the ``X-API-HASH`` value for a secret and nonce.

    :param api_secret: Your API secret. Never logged by this library.
    :param nonce: The same nonce sent in ``X-API-NONCE``.
    """
    digest = hashlib.sha512((api_secret + nonce).encode("utf-8")).digest()
    return _encode_digest(digest, encoding)


def build_auth_headers(
    *,
    api_token: str,
    api_secret: str,
    nonce: str,
    hash_encoding: HashEncoding = DEFAULT_HASH_ENCODING,
) -> dict[str, str]:
    """Build the three auth headers for one request."""
    if len(nonce) > NONCE_MAX_LENGTH:
        raise ValueError(
            f"Nonce is {len(nonce)} characters; the Huuray API accepts at most "
            f"{NONCE_MAX_LENGTH}. If you supplied a custom nonce_factory, shorten its output."
        )
    return {
        "X-API-TOKEN": api_token,
        "X-API-NONCE": nonce,
        "X-API-HASH": sign_request(api_secret, nonce, hash_encoding),
    }
