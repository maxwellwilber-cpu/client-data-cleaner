"""Tests for identity resolution."""

from cleaner.matching import resolve_identities


def record(name=None, dob=None, email=None, phone=None, placeholder=False,
           source="a.csv", row=0):
    return {
        "name_normalized": name, "date_of_birth": dob,
        "email_normalized": email, "phone_normalized": phone,
        "dob_is_placeholder": placeholder, "source_file": source, "source_row": row,
    }


def clusters_of(records, **kwargs):
    clusters, log = resolve_identities(records, **kwargs)
    return sorted(sorted(c) for c in clusters), log


class TestRules:
    def test_exact_name_and_dob_merges_at_full_confidence(self):
        recs = [record("maria lopez", "1975-09-02"), record("maria lopez", "1975-09-02")]
        clusters, log = clusters_of(recs)
        assert clusters == [[0, 1]]
        assert log[0]["confidence"] == 1.00
        assert log[0]["rule"] == "exact_name_exact_dob"

    def test_dob_off_by_one_day_still_merges(self):
        # Keystroke errors in a date field are extremely common.
        recs = [record("maria lopez", "1975-09-02"), record("maria lopez", "1975-09-03")]
        clusters, log = clusters_of(recs)
        assert clusters == [[0, 1]]
        assert log[0]["confidence"] == 0.95

    def test_same_name_and_phone_merges(self):
        recs = [record("david chen", phone="3105550142"),
                record("david chen", phone="3105550142")]
        clusters, log = clusters_of(recs)
        assert clusters == [[0, 1]]
        assert log[0]["rule"] == "exact_name_exact_phone"

    def test_typo_plus_same_email_merges(self):
        recs = [record("jonathan reyes", email="jr@x.com"),
                record("jonathon reyes", email="jr@x.com")]
        clusters, log = clusters_of(recs)
        assert clusters == [[0, 1]]
        assert log[0]["rule"] == "fuzzy_name_exact_email"


class TestSafety:
    def test_same_name_different_people_do_not_merge(self):
        # Two real Maria Lopezes with nothing else in common must stay separate.
        recs = [record("maria lopez", "1975-09-02"), record("maria lopez", "1991-03-18")]
        clusters, _ = clusters_of(recs)
        assert clusters == [[0], [1]]

    def test_placeholder_dob_is_not_used_as_evidence(self):
        # If both records say 1/1/1900, that agreement means nothing.
        recs = [record("john smith", "1900-01-01", placeholder=True),
                record("john smith", "1900-01-01", placeholder=True)]
        clusters, _ = clusters_of(recs)
        assert clusters == [[0], [1]]

    def test_name_alone_never_merges(self):
        recs = [record("kevin walker"), record("kevin walker")]
        clusters, _ = clusters_of(recs)
        assert clusters == [[0], [1]]

    def test_wildly_different_names_never_merge_on_email(self):
        # Shared email is strong, but not strong enough to join two unrelated names.
        recs = [record("maria lopez", email="shared@x.com"),
                record("thomas anderson", email="shared@x.com")]
        clusters, _ = clusters_of(recs)
        assert clusters == [[0], [1]]


class TestTransitivity:
    def test_a_matches_b_matches_c_becomes_one_person(self):
        # A and C share nothing directly; they are the same person only via B.
        recs = [
            record("jonathan reyes", "1988-04-12", email="jr@gmail.com", phone="3105550142"),
            record("jonathan reyes", "1988-04-13"),
            record("jonathon reyes", email="jr@gmail.com"),
        ]
        clusters, _ = clusters_of(recs)
        assert clusters == [[0, 1, 2]]


class TestConfidenceThreshold:
    def test_raising_the_bar_suppresses_weaker_merges(self):
        recs = [record("jonathan reyes", email="jr@x.com"),
                record("jonathon reyes", email="jr@x.com")]
        assert clusters_of(recs, min_confidence=0.85)[0] == [[0, 1]]
        assert clusters_of(recs, min_confidence=0.90)[0] == [[0], [1]]
