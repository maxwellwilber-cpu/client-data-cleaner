# client-data-cleaner

Turn several messy customer exports into one clean master list, with every merge logged
and the matching accuracy measured.

Small businesses accumulate the same customer in three systems under three spellings.
The booking software says *Jonathan Reyes*, the CRM says *Jon Reyes*, the payments sheet
says *Jonathon Reyes* because someone typed it wrong. Until those are one person, nobody
can answer "who are our best customers?" or "who have we lost?"

This does that, and then scores what it found.

```bash
pip install -r requirements.txt
python generate_sample_data.py   # builds realistic messy sample data
python run.py                    # runs the pipeline
python verify.py                 # measures how accurate the matching was
```

No real customer data is needed or included. The sample generator produces the mess.

> **macOS / Linux note:** if `python` is not found, use `python3` and `pip3`. macOS ships
> `python3` only. Inside a virtualenv (`python3 -m venv .venv && source .venv/bin/activate`)
> the bare `python` commands above work as written.

---

## What it does

```
 3 messy CSV exports          561 rows
          │
          ▼
   normalize every field      phones, emails, states, names, fake birthdays
          │
          ▼
   resolve identities         tiered rules, each merge scored and logged
          │
          ▼
   build golden records       329 people, best value per field
          │
          ▼
   attribute transactions     1,083 transactions → 100% linked to a person
          │
          ▼
   RFM segmentation           who is a Champion, who is At Risk, who is Lost
          │
          ▼
   4 output files             including a call list ranked by lifetime spend
```

## Results on the sample data

| | |
|---|---|
| Source rows in | 561 |
| People resolved | 329 |
| Duplicate rows collapsed | 232 |
| Merge operations (all logged) | 258 |
| Placeholder birthdays flagged | 43 |
| Transaction attribution rate | **100%** |
| Reactivation targets identified | 132 |

**Matching accuracy, measured against known ground truth:**

| | |
|---|---|
| Precision | **100.00%** — every merge it made was correct |
| Recall | **86.36%** — it found 86% of the real duplicates |
| F1 | **92.68%** |
| Correctly linked pairs | 266 |
| Wrong merges | **0** |

Run `python verify.py` to reproduce those numbers.

---

## The three ideas worth stealing

### 1. Normalize first, match second

Once `(310) 555-0142` and `+1 310-555-0142` both become `3105550142`, you do not need
fuzzy matching to see they are the same number. Cheap deterministic cleanup removes most
of the work before any clever logic runs.

The normalizers handle what real exports actually contain: six phone formats, Gmail
dot-aliases and `+tags` (`john.doe+receipts@gmail.com` is the same inbox as
`johndoe@gmail.com`), 40+ ways to write a state, accents and apostrophes, and placeholder
birthdays like `1/1/1900` and `12/31/1969` — the second being Unix epoch bleed-through
from a system that stored zero as a timestamp.

### 2. Tiered rules with explicit confidence, not one fuzzy score

| Confidence | Rule |
|---|---|
| 1.00 | same name + same date of birth |
| 0.95 | same name + DOB within one day (keystroke errors) |
| 0.90 | same name + same phone |
| 0.85 | name within edit distance 2 + same email (typos) |

When a merge is wrong you need to know *which rule* fired so you can fix that rule. A
single blended similarity score tells you nothing and cannot be tuned safely. Every merge
here is traceable to one rule and one number, and `merge_log.csv` records all of them.

Placeholder birthdays are excluded as evidence — if two records both say `1/1/1900`, that
agreement means nothing and must not be treated as a match.

### 3. Measure the matching instead of asserting it

Any dedup script can produce a smaller file. The question nobody usually answers is
whether it merged the *right* rows.

`verify.py` scores the matcher against data whose true answers are known, using pairwise
precision and recall, and reports the hit rate **per rule**.

It earned its place immediately. The first version of the sample generator gave two
different people the same Gmail address, and the fuzzy-name rule promptly scored **133
wrong merges against 80 right ones**. The rule was fine; the fake data was impossible.
Without the harness that would have shipped as a quietly broken matcher — or worse, as a
confident accuracy claim in a README.

The tuning favours precision over recall on purpose. Merging two different customers is
worse than missing a duplicate: a wrong merge emails the wrong person about someone
else's account, while a missed duplicate just leaves a customer listed twice. The 42
duplicates it misses are records with a typo'd name and no email, phone, or birthday —
genuinely unmatchable without guessing.

---

## Output files

| File | What it is |
|---|---|
| `clients_master.csv` | One row per real person, best value per field, with source lineage |
| `customer_segments.csv` | RFM scores and behavioural segment per person |
| `reactivation_targets.csv` | Lapsed customers ranked by lifetime spend — a call list |
| `merge_log.csv` | Every merge with its rule and confidence |

`reactivation_targets.csv` is the one a business actually uses. Not a dashboard: a list
the front desk can start working, ranked by money, so if only 200 calls get made they are
the 200 most valuable ones.

## Using it on real data

```bash
python run.py --clients crm.csv booking.csv payments.csv \
              --transactions sales.csv \
              --output results/ \
              --min-confidence 0.90
```

Column names are detected from common aliases, so `email` / `email_address` / `e_mail`
all work, and files with different schemas can be mixed. Raise `--min-confidence` to merge
more conservatively.

## Tests

```bash
python -m pytest tests/ -v
```

62 tests. The ones that matter most are in `TestSafety` — they assert what the matcher
must *never* do: merge two people who share only a name, treat a placeholder birthday as
evidence, or join unrelated names on a shared email.

## Structure

```
cleaner/
  normalize.py   field-level cleaning, deterministic
  matching.py    tiered identity resolution + union-find clustering
  rfm.py         quantile scoring and segmentation
  pipeline.py    orchestration, golden records, attribution
generate_sample_data.py   realistic messy data + hidden ground truth
run.py                    entry point
verify.py                 accuracy measurement
tests/                    62 pytest tests
```

## Related

- [evs](https://github.com/maxwellwilber-cpu/evs) — a 43-test framework for validating
  AI-generated analysis output. Same idea applied to LLM pipelines: check the work rather
  than trust it.

MIT licensed. Built by [Maxwell Wilber](https://linkedin.com/in/maxwellwilber).
