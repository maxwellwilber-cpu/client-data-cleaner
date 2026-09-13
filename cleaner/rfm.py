"""
RFM scoring and behavioral segmentation.

RFM asks three questions about every customer:

    Recency   - how long since they last bought?
    Frequency - how often do they buy?
    Monetary  - how much have they spent?

Each gets a 1-5 score, and 5 is always the good end: 5 = bought recently, buys often,
spends a lot. Keeping "5 is good" consistent across all three is what makes the segment
rules readable. Flipping recency (as some implementations do) saves nothing and causes
a steady trickle of bugs.

Scores are quantiles, not fixed thresholds. "Spent over $500" means something different
for a dental practice than a coffee shop, but "top 20% of spenders" means the same thing
everywhere. Quantiles make the tool portable across businesses without configuration.
"""

import pandas as pd

# Segment rules, evaluated top to bottom; first match wins.
# Ordering matters: Champions must be tested before Loyal, or every Champion
# would be labelled Loyal instead.
_SEGMENT_RULES = [
    ("Champions",        lambda r, f, m: r >= 4 and f >= 4),
    ("Can't Lose Them",  lambda r, f, m: r <= 2 and f >= 4 and m >= 4),
    ("Loyal",            lambda r, f, m: r >= 3 and f >= 3),
    ("At Risk",          lambda r, f, m: r <= 2 and f >= 3),
    ("New Customers",    lambda r, f, m: r == 5 and f <= 1),
    ("Potential Loyalists", lambda r, f, m: r >= 4 and f == 2),
    ("Promising",        lambda r, f, m: r >= 4 and f <= 1),
    ("Need Attention",   lambda r, f, m: r == 3 and f == 2),
    ("About to Sleep",   lambda r, f, m: r == 3),
    ("Lost",             lambda r, f, m: r <= 2),
]

# Segments worth contacting in a win-back campaign, most valuable first.
REACTIVATION_SEGMENTS = ["Can't Lose Them", "At Risk", "About to Sleep", "Lost"]


def _quantile_score(series, reverse=False):
    """
    Bucket a numeric column into scores 1-5 by quantile.

    reverse=True is for recency, where a LOW number of days is good.

    Falls back to ranking when a column has heavy ties (for example, most customers
    having exactly one purchase). qcut raises on duplicate bin edges; ranking first
    spreads the ties out so every customer still lands in a bucket.
    """
    try:
        scores = pd.qcut(series, 5, labels=[1, 2, 3, 4, 5])
        scores = scores.astype(int)
    except ValueError:
        ranked = series.rank(method="first")
        scores = pd.qcut(ranked, 5, labels=[1, 2, 3, 4, 5]).astype(int)
    if reverse:
        scores = 6 - scores
    return scores


def score_customers(transactions, as_of=None, customer_col="person_id",
                    date_col="transaction_date", amount_col="amount"):
    """
    Turn a transaction log into a scored customer table.

    Args:
        transactions: DataFrame with one row per purchase.
        as_of: the date recency is measured from. Defaults to the latest
               transaction in the data, which keeps results reproducible — using
               "today" would make the same input produce different output tomorrow.

    Returns:
        DataFrame indexed by customer with recency_days, frequency, monetary,
        the three 1-5 scores, rfm_score, and segment.
    """
    df = transactions.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])
    if df.empty:
        raise ValueError("No transactions with a usable date.")

    reference = pd.to_datetime(as_of) if as_of else df[date_col].max()

    customers = df.groupby(customer_col).agg(
        last_transaction=(date_col, "max"),
        frequency=(date_col, "count"),
        monetary=(amount_col, "sum"),
    )
    customers["recency_days"] = (reference - customers["last_transaction"]).dt.days

    customers["r_score"] = _quantile_score(customers["recency_days"], reverse=True)
    customers["f_score"] = _quantile_score(customers["frequency"])
    customers["m_score"] = _quantile_score(customers["monetary"])

    # The concatenated triplet, e.g. "545". Useful for eyeballing and for grouping
    # customers who behave identically.
    customers["rfm_score"] = (
        customers["r_score"].astype(str)
        + customers["f_score"].astype(str)
        + customers["m_score"].astype(str)
    )
    customers["segment"] = [
        assign_segment(r, f, m)
        for r, f, m in zip(customers["r_score"], customers["f_score"], customers["m_score"])
    ]
    return customers.reset_index()


def assign_segment(r_score, f_score, m_score):
    """Return the first segment whose rule matches. Falls back to 'Others'."""
    for name, rule in _SEGMENT_RULES:
        if rule(r_score, f_score, m_score):
            return name
    return "Others"


def reactivation_targets(scored, min_monetary_score=1):
    """
    Lapsed customers worth contacting, ranked by lifetime spend.

    This is the output a business actually uses: not a dashboard, a list the front
    desk can start calling. Ranked by money because if only 200 calls get made, they
    should be the 200 most valuable ones.
    """
    targets = scored[scored["segment"].isin(REACTIVATION_SEGMENTS)].copy()
    targets = targets[targets["m_score"] >= min_monetary_score]
    return targets.sort_values("monetary", ascending=False).reset_index(drop=True)
