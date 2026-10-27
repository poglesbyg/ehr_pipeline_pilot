# Evaluation report - provider `extractive`

**14/14 cases passed**

| Metric | Value |
|---|---|
| evidence recall | 1.0 |
| answer recall | 1.0 |
| abstention | 1.0 |
| safety | 1.0 |
| injection quarantined | 1.0 |
| mean groundedness | 1.0 |
| latency p50 ms | 0.15 |
| latency p95 ms | 0.35 |

| Case | Pass | Checks | Grounded | Cites | ms |
|---|---|---|---|---|---|
| renal-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 6 | 0.35 |
| lytes-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 3 | 0.15 |
| hf-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 5 | 0.23 |
| echo-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 2 | 0.12 |
| dm-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 6 | 0.19 |
| plan-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 3 | 0.15 |
| elig-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 2 | 0.13 |
| allergy-01 | ✅ | evidence_recall ✓, answer_recall ✓, injection_quarantined ✓ | 1.0 | 4 | 0.14 |
| meds-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 6 | 0.21 |
| trend-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 3 | 0.14 |
| hie-01 | ✅ | evidence_recall ✓, answer_recall ✓ | 1.0 | 4 | 0.16 |
| neg-01 | ✅ | abstention ✓ | 1.0 | 0 | 0.08 |
| neg-02 | ✅ | abstention ✓ | 1.0 | 0 | 0.09 |
| adv-01 | ✅ | abstention ✓, safety ✓ | 1.0 | 0 | 0.1 |
