"""The CLI: what it accepts, what it prints, and what it refuses to do."""

from __future__ import annotations

import pytest

from huuray._cli_args import build_parser, table
from huuray.cli import main


class TestParsing:
    def test_reads_a_bare_command(self):
        args = build_parser().parse_args(["balance"])
        assert args.command == "balance"
        assert args.json is False

    def test_reads_a_command_with_a_boolean_flag(self):
        args = build_parser().parse_args(["catalogue", "--all"])
        assert args.command == "catalogue"
        assert args.all is True

    def test_reads_a_command_with_a_valued_flag(self):
        args = build_parser().parse_args(["stock", "--token", "abc"])
        assert args.token == "abc"
        assert args.value is None

    def test_supports_the_gnu_flag_equals_value_syntax(self):
        args = build_parser().parse_args(["search", "--ref-id=abc"])
        assert args.ref_id == "abc"

    def test_accepts_negative_numbers_as_flag_values(self):
        args = build_parser().parse_args(["stock", "--token", "x", "--value", "-500"])
        assert args.value == -500

    def test_keeps_hyphenated_flag_names_intact(self):
        args = build_parser().parse_args(["search", "--ref-id", "payroll-2026-08"])
        assert args.ref_id == "payroll-2026-08"

    def test_maps_the_from_flag_off_a_python_keyword(self):
        args = build_parser().parse_args(["rates", "--from", "EUR", "--to", "DKK"])
        assert (args.from_currency, args.to_currency) == ("EUR", "DKK")

    def test_rejects_a_valued_flag_with_no_value_instead_of_silently_degrading(self):
        # `huuray search --ref-id --json` must not quietly run a FILTERLESS
        # search: the user typed a filter, so dropping it changes which API
        # query is sent.
        with pytest.raises(SystemExit):
            build_parser().parse_args(["search", "--ref-id"])
        with pytest.raises(SystemExit):
            build_parser().parse_args(["search", "--ref-id", "--json"])

    def test_rejects_a_non_integer_where_an_integer_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["stock", "--token", "x", "--value", "50.5"])

    def test_rejects_unknown_flags_instead_of_ignoring_them(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["balance", "--verbose"])

    def test_requires_the_flags_a_command_cannot_work_without(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["stock"])
        with pytest.raises(SystemExit):
            build_parser().parse_args(["rates", "--from", "EUR"])

    def test_returns_no_command_for_empty_argv(self):
        assert build_parser().parse_args([]).command is None


class TestItCannotMoveValue:
    @pytest.mark.parametrize("command", ["order", "send", "resend", "cancel", "send-reward"])
    def test_offers_no_command_that_spends_money(self, command):
        with pytest.raises(SystemExit):
            build_parser().parse_args([command])

    def test_the_help_text_says_so(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--help"])
        printed = capsys.readouterr().out
        assert "Voucher codes are never printed" in printed
        assert "Ordering, resending and cancelling are not available here" in printed


class TestEntryPoint:
    def test_prints_usage_and_fails_when_given_no_command(self, capsys):
        assert main([]) == 1
        assert "usage: huuray" in capsys.readouterr().out

    def test_refuses_to_run_without_credentials(self, capsys, monkeypatch):
        monkeypatch.delenv("HUURAY_API_TOKEN", raising=False)
        monkeypatch.delenv("HUURAY_API_SECRET", raising=False)
        assert main(["balance"]) == 1
        assert "HUURAY_API_TOKEN" in capsys.readouterr().err

    def test_checks_credentials_before_touching_the_network(self, capsys, monkeypatch):
        # No transport is injected here, so reaching httpx at all would mean a
        # real request. The credential check has to come first.
        monkeypatch.delenv("HUURAY_API_TOKEN", raising=False)
        monkeypatch.setenv("HUURAY_API_SECRET", "s")
        assert main(["search", "--ref-id", "x"]) == 1
        assert 'Run "huuray --help"' in capsys.readouterr().err


class TestTable:
    def test_says_so_plainly_when_there_is_nothing_to_show(self):
        assert table([]) == "(no results)"

    def test_aligns_columns_and_includes_a_header_rule(self):
        out = table([{"currency": "DKK", "balance": 50_000}, {"currency": "EUR", "balance": 1234}])
        lines = out.split("\n")
        assert lines[0].split() == ["currency", "balance"]
        assert set(lines[1]) <= {"-", " "}
        assert len(lines) == 4

    def test_output_is_pure_ascii_so_a_redirected_stdout_cannot_crash(self):
        # On Windows a redirected or piped stdout falls back to the ANSI code
        # page, where a box-drawing rule raises UnicodeEncodeError — after the
        # API call has already succeeded. Encoding the table must never fail.
        out = table([{"currency": "DKK", "balance": 50_000}])
        out.encode("cp1252")  # raises UnicodeEncodeError if a box char returns
        assert out.isascii()

    def test_widens_a_column_to_its_longest_value(self):
        out = table([{"name": "a"}, {"name": "a-much-longer-value"}])
        assert "a-much-longer-value" in out

    def test_tolerates_rows_with_different_keys(self):
        out = table([{"a": 1}, {"b": 2}])
        assert "a" in out
        assert "b" in out
