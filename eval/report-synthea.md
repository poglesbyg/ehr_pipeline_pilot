# Evaluation report - provider `extractive`

**23/23 cases passed** · scope attacks blocked n/a

| Metric | Value |
|---|---|
| retrieval recall at k | 1.0 |
| answer recall on retrieval successes | 1.0 |
| abstention | 1.0 |
| safety | None |
| injection quarantined | None |
| risk flags raised | None |
| mean groundedness | 1.0 |
| medication statements | 18 |
| fabricated medications | 0 |
| fabricated rate 95 upper | 0.167 |
| latency p50 ms | 3.4 |
| latency p95 ms | 4.38 |

## By stratum

| Stratum | Passed | Retrieval recall@k | Groundedness |
|---|---|---|---|
| synthea-absent | 4/4 | None | 1.0 |
| synthea-condition | 4/4 | 1.0 | 1.0 |
| synthea-latest-lab | 3/3 | 1.0 | 1.0 |
| synthea-meds | 4/4 | 1.0 | 1.0 |
| synthea-resolved | 4/4 | 1.0 | 1.0 |
| synthea-vaccine | 4/4 | 1.0 | 1.0 |

## Scope attacks

| Attack | Blocked | Detail |
|---|---|---|

## Cases

| Case | Stratum | User | Pass | Checks | Grounded | Cites | ms |
|---|---|---|---|---|---|---|---|
| 12edb5a0-latest-lab | synthea-latest-lab | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 4.38 |
| 12edb5a0-meds | synthea-meds | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.84 |
| 12edb5a0-condition | synthea-condition | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 1 | 3.4 |
| 12edb5a0-resolved | synthea-resolved | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.55 |
| 12edb5a0-absent | synthea-absent | dr-primary | ✅ | abstention ✓ | 1.0 | 0 | 3.76 |
| 12edb5a0-vaccine | synthea-vaccine | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 6.04 |
| 1f456f0a-meds | synthea-meds | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.37 |
| 1f456f0a-condition | synthea-condition | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 1 | 2.94 |
| 1f456f0a-resolved | synthea-resolved | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.02 |
| 1f456f0a-absent | synthea-absent | dr-primary | ✅ | abstention ✓ | 1.0 | 0 | 2.96 |
| 1f456f0a-vaccine | synthea-vaccine | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.02 |
| 351502fd-latest-lab | synthea-latest-lab | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 4.21 |
| 351502fd-meds | synthea-meds | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.53 |
| 351502fd-condition | synthea-condition | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.39 |
| 351502fd-resolved | synthea-resolved | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.37 |
| 351502fd-absent | synthea-absent | dr-primary | ✅ | abstention ✓ | 1.0 | 0 | 3.15 |
| 351502fd-vaccine | synthea-vaccine | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.48 |
| f12a1a30-latest-lab | synthea-latest-lab | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 4.04 |
| f12a1a30-meds | synthea-meds | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.46 |
| f12a1a30-condition | synthea-condition | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.61 |
| f12a1a30-resolved | synthea-resolved | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.4 |
| f12a1a30-absent | synthea-absent | dr-primary | ✅ | abstention ✓ | 1.0 | 0 | 3.05 |
| f12a1a30-vaccine | synthea-vaccine | dr-primary | ✅ | retrieval_recall ✓, answer_recall ✓ | 1.0 | 6 | 3.29 |
