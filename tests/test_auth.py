"""Nonce generation and request signing."""

from __future__ import annotations

import hashlib
import re

import pytest

from huuray import (
    DEFAULT_HASH_ENCODING,
    NONCE_MAX_LENGTH,
    build_auth_headers,
    generate_nonce,
    sign_request,
)


class TestNonceGeneration:
    def test_stays_within_the_50_character_limit_the_api_enforces(self):
        for _ in range(1000):
            assert len(generate_nonce()) <= NONCE_MAX_LENGTH

    def test_produces_32_base64url_characters(self):
        nonce = generate_nonce()
        assert len(nonce) == 32
        assert re.fullmatch(r"[A-Za-z0-9_-]+", nonce)

    def test_does_not_repeat_the_api_rejects_a_reused_nonce_for_60_days(self):
        seen = {generate_nonce() for _ in range(100_000)}
        assert len(seen) == 100_000

    def test_rejects_a_custom_nonce_that_would_exceed_the_api_limit(self):
        with pytest.raises(ValueError, match="at most 50"):
            build_auth_headers(api_token="t", api_secret="s", nonce="x" * 51)

    def test_accepts_a_nonce_exactly_at_the_limit(self):
        headers = build_auth_headers(api_token="t", api_secret="s", nonce="x" * 50)
        assert headers["X-API-NONCE"] == "x" * 50

    def test_rejects_32_byte_hex_the_classic_over_limit_mistake(self):
        with pytest.raises(ValueError):
            build_auth_headers(api_token="t", api_secret="s", nonce="a" * 64)


class TestRequestSigning:
    @staticmethod
    def expected(secret: str, nonce: str) -> str:
        # Computed here independently rather than copied from the
        # implementation, so this fails if the construction changes.
        return hashlib.sha512((secret + nonce).encode("utf-8")).hexdigest()

    def test_is_sha512_over_api_secret_then_nonce_in_that_order(self):
        assert sign_request("sec", "non") == self.expected("sec", "non")

    def test_is_order_sensitive_nonce_plus_secret_is_a_different_digest(self):
        assert sign_request("ab", "cd") != sign_request("cd", "ab")

    def test_defaults_to_lowercase_hex(self):
        assert DEFAULT_HASH_ENCODING == "hex"
        assert re.fullmatch(r"[0-9a-f]{128}", sign_request("sec", "non"))

    @pytest.mark.parametrize(
        ("encoding", "pattern"),
        [
            ("hex", r"[0-9a-f]{128}"),
            ("hex-upper", r"[0-9A-F]{128}"),
            ("base64", r"[A-Za-z0-9+/]+=*"),
            ("base64url", r"[A-Za-z0-9_-]+"),
        ],
    )
    def test_supports_every_documented_encoding(self, encoding, pattern):
        assert re.fullmatch(pattern, sign_request("sec", "non", encoding))

    def test_rejects_an_unknown_encoding_rather_than_guessing(self):
        with pytest.raises(ValueError, match="Unknown hash encoding"):
            sign_request("sec", "non", "rot13")  # type: ignore[arg-type]

    def test_uses_the_encoding_confirmed_against_the_live_api(self):
        # The v4 specification states the construction, SHA512(API_SECRET +
        # NONCE), but not the digest encoding. Confirmed live on 2026-08-15:
        # lowercase hex authenticated against GET /v4/Balance on
        # api.huuray.com (the other three candidate encodings returned 401).
        # If this fails, someone changed the default — that breaks every
        # consumer unless the API changed first.
        assert DEFAULT_HASH_ENCODING == "hex"


class TestAuthHeaders:
    def test_sends_exactly_the_three_documented_headers(self):
        headers = build_auth_headers(api_token="tok", api_secret="sec", nonce="abc")
        assert sorted(headers) == ["X-API-HASH", "X-API-NONCE", "X-API-TOKEN"]
        assert headers["X-API-TOKEN"] == "tok"
        assert headers["X-API-NONCE"] == "abc"

    def test_never_puts_the_secret_in_a_header(self):
        headers = build_auth_headers(api_token="tok", api_secret="super-secret", nonce="abc")
        assert "super-secret" not in str(headers)
