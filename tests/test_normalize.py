"""Tests for field normalization."""

import pytest

from cleaner.normalize import (
    normalize_phone, normalize_email, normalize_state, normalize_name,
    is_placeholder_dob, levenshtein,
)


class TestPhone:
    @pytest.mark.parametrize("raw", [
        "(310) 555-0142", "310-555-0142", "310.555.0142",
        "+1 310 555 0142", "1-310-555-0142", "3105550142", " 310 555 0142 ",
    ])
    def test_all_formats_collapse_to_same_digits(self, raw):
        assert normalize_phone(raw) == "3105550142"

    @pytest.mark.parametrize("raw", [
        "555-0142",       # too short
        "12345678901234", # too long
        "010-555-0142",   # area code cannot start with 0
        "310-055-0142",   # exchange cannot start with 0
        "not a phone", "", None,
    ])
    def test_rejects_impossible_numbers(self, raw):
        # A wrong phone number is worse than none: it creates false matches.
        assert normalize_phone(raw) is None


class TestEmail:
    def test_lowercases_and_trims(self):
        assert normalize_email("  John@Example.COM ") == "john@example.com"

    def test_gmail_dots_and_tags_are_ignored(self):
        # These are all literally the same Gmail inbox.
        assert normalize_email("john.doe@gmail.com") == "johndoe@gmail.com"
        assert normalize_email("johndoe+receipts@gmail.com") == "johndoe@gmail.com"
        assert normalize_email("J.Doe@googlemail.com") == "jdoe@gmail.com"

    def test_dots_are_preserved_outside_gmail(self):
        # Other providers treat dots as significant, so stripping them would be wrong.
        assert normalize_email("j.smith@outlook.com") == "j.smith@outlook.com"

    @pytest.mark.parametrize("raw", ["notanemail", "no@domain", "a@b@c.com", "@gmail.com", "", None])
    def test_rejects_malformed(self, raw):
        assert normalize_email(raw) is None


class TestState:
    @pytest.mark.parametrize("raw", ["CA", "ca", "California", "Calif.", "cali", " ca "])
    def test_california_variants(self, raw):
        assert normalize_state(raw) == "CA"

    def test_unknown_returns_none(self):
        assert normalize_state("Atlantis") is None


class TestName:
    def test_strips_accents_and_punctuation(self):
        assert normalize_name("José O'Brien") == "jose obrien"

    def test_collapses_whitespace_and_case(self):
        assert normalize_name("  MARIA   LOPEZ ") == "maria lopez"

    def test_empty_is_none(self):
        assert normalize_name("   ") is None


class TestPlaceholderDob:
    @pytest.mark.parametrize("raw", ["1900-01-01", "1990-01-01", "1969-12-31", "1970-01-01"])
    def test_detects_known_fillers(self, raw):
        assert is_placeholder_dob(raw) is True

    def test_real_birthday_passes(self):
        assert is_placeholder_dob("1988-04-12") is False


class TestLevenshtein:
    def test_identical_is_zero(self):
        assert levenshtein("reyes", "reyes") == 0

    def test_single_substitution(self):
        assert levenshtein("jonathon", "jonathan") == 1

    def test_handles_empty(self):
        assert levenshtein("", "abc") == 3
