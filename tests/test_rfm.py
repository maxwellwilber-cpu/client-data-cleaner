"""Tests for RFM scoring and segmentation."""

import pandas as pd
import pytest

from cleaner.rfm import score_customers, assign_segment, reactivation_targets


def build_transactions():
    """
    A small book with a GRADED spread of behaviour.

    The spread matters: quantile scoring needs variation to be meaningful. An earlier
    version of this fixture gave 25 customers exactly 10 purchases and 25 exactly 1,
    and the tie-breaking fallback then scattered identical customers across buckets
    3, 4 and 5 — correct behaviour, but it made the test assert something false.
    Customer 1 is the best here, customer 50 the worst, and everyone in between is
    ordered.
    """
    rows = []
    for person_id in range(1, 51):
        # Frequency falls smoothly from 12 purchases down to 1.
        count = max(1, 13 - (person_id // 4))
        # Recency and spend degrade with id as well.
        base_days_ago = (person_id - 1) * 18
        amount = 400 - (person_id * 6)
        for n in range(count):
            rows.append({
                "person_id": person_id,
                "transaction_date": pd.Timestamp("2026-09-01")
                - pd.Timedelta(days=base_days_ago + n * 10),
                "amount": amount,
            })
    return pd.DataFrame(rows)


class TestScoring:
    def test_every_customer_gets_scored(self):
        scored = score_customers(build_transactions())
        assert len(scored) == 50
        for column in ("r_score", "f_score", "m_score", "rfm_score", "segment"):
            assert column in scored.columns

    def test_scores_stay_in_range(self):
        scored = score_customers(build_transactions())
        for column in ("r_score", "f_score", "m_score"):
            assert scored[column].between(1, 5).all()

    def test_five_is_always_the_good_end(self):
        scored = score_customers(build_transactions()).set_index("person_id")
        # Customer 1 buys often, recently, and spends more.
        assert scored.loc[1, "r_score"] >= 4
        assert scored.loc[1, "f_score"] >= 4
        # Customer 50 bought once, ages ago.
        assert scored.loc[50, "r_score"] <= 2
        assert scored.loc[50, "f_score"] <= 2

    def test_results_are_reproducible(self):
        # Recency is measured from the data's own latest date, not today, so the same
        # input must always give the same output.
        first = score_customers(build_transactions())
        second = score_customers(build_transactions())
        pd.testing.assert_frame_equal(first, second)

    def test_empty_input_raises_clearly(self):
        empty = pd.DataFrame({"person_id": [], "transaction_date": [], "amount": []})
        with pytest.raises(ValueError):
            score_customers(empty)


class TestSegments:
    def test_best_customers_are_champions(self):
        assert assign_segment(5, 5, 5) == "Champions"

    def test_valuable_but_lapsed_is_cant_lose(self):
        assert assign_segment(1, 5, 5) == "Can't Lose Them"

    def test_gone_and_low_value_is_lost(self):
        assert assign_segment(1, 1, 1) == "Lost"

    def test_every_possible_triplet_gets_a_segment(self):
        # No customer may fall through the rules unlabelled.
        for r in range(1, 6):
            for f in range(1, 6):
                for m in range(1, 6):
                    assert assign_segment(r, f, m) not in (None, "")


class TestReactivation:
    def test_targets_are_lapsed_only(self):
        scored = score_customers(build_transactions())
        targets = reactivation_targets(scored)
        assert (targets["r_score"] <= 3).all()

    def test_targets_are_ranked_by_money(self):
        scored = score_customers(build_transactions())
        targets = reactivation_targets(scored)
        assert targets["monetary"].is_monotonic_decreasing
