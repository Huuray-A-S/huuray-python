"""Redaction — the last line of defence against a voucher code in a log."""

from __future__ import annotations

from typing import Any

from huuray import Recipient, Voucher, redact, safe_json


class TestRedaction:
    def test_removes_voucher_codes_they_are_bearer_instruments(self):
        out = safe_json(
            {
                "Vouchers": [
                    {
                        "ID": 1,
                        "Code": "REAL-CODE-123",
                        "CVV": "999",
                        "RedeemLink": "https://r/abc",
                    }
                ]
            }
        )
        assert "REAL-CODE-123" not in out
        assert "999" not in out
        assert "https://r/abc" not in out
        assert "[redacted: bearer value]" in out

    def test_redacts_the_mapped_snake_case_fields_too(self):
        out = safe_json({"vouchers": [{"code": "REAL", "cvv": "1", "redeem_link": "https://x"}]})
        assert "REAL" not in out
        assert "https://x" not in out

    def test_redacts_dataclass_results_not_only_raw_bodies(self):
        voucher = Voucher(
            id=1,
            code="REAL-CODE",
            cvv="999",
            redeem_link="https://r/1",
            expires="2027-01-01",
            recipient=Recipient(name="Jane", email="jane@example.com"),
        )
        out = safe_json(voucher)
        assert "REAL-CODE" not in out
        assert "jane@example.com" not in out
        assert "2027-01-01" in out

    def test_keeps_ids_and_expiry_which_are_safe_and_useful_in_a_log(self):
        out = redact({"ID": 42, "Expires": "2027-01-01", "Code": "SECRET"})
        assert out["ID"] == 42
        assert out["Expires"] == "2027-01-01"
        assert out["Code"] == "[redacted: bearer value]"

    def test_masks_personal_data_without_destroying_it_entirely(self):
        out = redact({"Email": "jane@example.com"})
        assert out["Email"] != "jane@example.com"
        assert out["Email"] == "ja***om"

    def test_masks_short_personal_data_completely(self):
        assert redact({"Phone": "1234"})["Phone"] == "***"

    def test_masks_credentials(self):
        out = safe_json({"api_token": "tok_live_abcdef", "api_secret": "shhh-secret"})
        assert "tok_live_abcdef" not in out
        assert "shhh-secret" not in out

    def test_masks_the_auth_headers(self):
        out = safe_json({"X-API-TOKEN": "tok_live_abcdef", "X-API-HASH": "deadbeef" * 16})
        assert "tok_live_abcdef" not in out

    def test_leaves_empty_and_none_values_alone_rather_than_inventing_a_marker(self):
        out = redact({"Code": None, "CVV": ""})
        assert out["Code"] is None
        assert out["CVV"] == ""

    def test_walks_nested_structures(self):
        assert "DEEP" not in safe_json({"a": {"b": {"c": [{"Code": "DEEP"}]}}})

    def test_does_not_recurse_forever_on_a_cycle(self):
        cyclic: dict[str, Any] = {"name": "x"}
        cyclic["self"] = cyclic
        assert redact(cyclic)["name"] == "x"

    def test_leaves_scalars_untouched(self):
        assert redact(42) == 42
        assert redact("plain") == "plain"
        assert redact(None) is None
