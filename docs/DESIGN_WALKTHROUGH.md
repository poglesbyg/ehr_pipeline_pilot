# Design walkthrough: search and summarization over a veteran's record

This is the design answer to "design search and summarization over a veteran's record," with
every claim tied to the code and tests in this repository that back it. The production
column describes the target pattern in Azure Government. Those parts are designed but not
built here; they are marked as such.

---

## 1. Clarify

- **Who uses it, for what:** visit prep (summary), or questions during the visit (Q&A)?
- **Pilot scope:** which sites, and are they on VistA, Oracle Health, or both?
- **Sources in version one:** notes and structured data first, faxes second?
- **Latency target:** and is a streamed answer acceptable?
- **Boundary:** is there an existing Azure Government boundary, and what already has an ATO?

Default assumptions if they say "you decide": primary care visit prep, one pilot site, notes
and structured data first with faxes in phase two, a streamed answer within about ten seconds,
and an existing Azure Government boundary.

## 2. Size it (illustrative numbers)

A complex patient might have about 3,000 documents averaging 1,000 tokens: 3 million tokens,
far beyond any context window, so retrieval is required. At about 500 tokens a chunk that is
around 6,000 chunks for one patient. Indexing every veteran up front is a large, expensive job
before value is proven, so **version one indexes on demand**: when a chart opens (or overnight
for tomorrow's scheduled patients), cached, and expired on a schedule.

*In this repo:* `ClinicalAssistant._ensure_indexed` loads a patient's record on first request,
caches it for `cache_ttl`, and drops it when new data for that patient commits
(`tests/test_scope.py::test_indexing_is_on_demand_and_invalidated_on_commit`). The working set
for a request holds only that patient's chunks.

## 3. The flow

```
 VistA (HL7 v2 / MLLP)    Oracle Health (FHIR)    HIE (C-CDA)    Faxes (OCR)
          │                       │                    │              │
          └───────────────────────┴─────────┬──────────┴──────────────┘
                                            ▼
          Ingestion: parse · map to US Core FHIR · validate · label 7332 · provenance
                                            ▼
                Index: chunks with ICN, source, date, note type, OCR confidence, labels
                                            ▼
     Orchestrator: access check → scoped retrieval → second scope check → screen →
                   generate (numbered, dated evidence) → verify in code → audit
                                            ▼
              Clinician: answer with every statement one click from its source
```

| Stage | This repo | Production target |
|---|---|---|
| VistA feed | `mllp.py`, `hl7v2.py`, `mapper.py`: ADT, ORU, MDM incl. T11 retractions | Same contract, or an integration engine that passes the same golden tests |
| Oracle Health / Epic | `fhir_client.py` + `fhir_import.py` + SMART launch (`smart.py`) | Same, plus Backend Services and Bulk `$export` |
| HIE | `ccda.py` (`POST /hie/ccda`) | XCA query/retrieve, same mapping |
| Faxes | OCR text with per-page confidence (simulated) | Document Intelligence layout + confidence |
| Index | `ai/index.py` BM25 with synonyms and context headers | Azure AI Search: hybrid keyword + vector, semantic ranker, patient prefilter |
| Model | `extractive`, `ollama`, `azure-openai` providers | Azure OpenAI in a Government region, private endpoint, managed identity |

## 4. Deep dive A: patient scope, end to end

A prompt instruction is not a security control. Scope is enforced before the model, not by it.

| Step | Control | Code | Test |
|---|---|---|---|
| Sign in | Clinician identity from SSO, never from the request | `POST /launch` (SSO stand-in), `AccessPolicy` | `test_mllp_round_trip_then_fhir_search_and_ask` |
| Patient context | From the EHR launch, held server-side; `/ai/ask` ignores any patient in the body. Real SMART App Launch (PKCE): the token's patient is the access decision | `App.complete_smart`, `smart.py`, session cookie | `test_ehr_launch_end_to_end`, `test_launched_user_sees_only_the_launched_patient` |
| ICN validation | `\d{10}V\d{6}` before use; also blocks filter injection | `access.valid_icn`, `resolve_icn` | `test_icn_validation_blocks_filter_injection` |
| Access check | Clinician may see this patient, or nothing runs; denials audited | `ClinicalAssistant.authorize` | `test_denials_fail_closed_and_are_audited` |
| Filter in the index | Patient identity and readable 7332 labels applied before scoring | `EvidenceIndex.search(allow=...)` | `test_protected_records_hidden_without_authorization` |
| 38 USC 7332 | Substance use, HIV, sickle cell labeled at ingestion (`meta.security`) | `sensitivity.py` | `test_labels_assigned_at_ingestion` |
| Second check after retrieval | Every hit re-checked; a foreign or unreadable chunk fails closed | `_verify_scope` → `ScopeViolation` | `test_post_retrieval_check_catches_a_filter_bug` |
| Model call | Only that patient's screened evidence leaves the index | `ask()` | eval scope attacks |
| Audit | Who, which patient, query, evidence disclosed, model and version; denials too | `_audit` | `test_entra_token_retry_on_throttling_and_model_version_audited` |

Attack suite, run on every eval (`python -m hl7bridge eval`): another patient's ICN, a
malformed ICN aimed at the filter, a clinician with no relationship, an unknown user, a 7332
record without authorization, and a simulated filter bug that injects another patient's chunk.
Every one must return nothing; the current report shows 6/6 blocked.

Emergency access: follow the EHR's own access decision and break-the-glass policy rather than
inventing a separate one, and log every use. The MCP server follows the same rules: the agent
acts for one clinician, `--launch-icn` binds it to one patient, and `find_patient` only
reveals patients that clinician may see.

## 5. Deep dive B: knowing the answers are right

| Step | This repo | Production |
|---|---|---|
| Test set | `eval/gold.json`: 18 cases stratified by source type, incl. unanswerable, injected instructions, low-confidence OCR, copied-forward text, 7332 | 100–200 clinician-written questions, stratified by site, EHR, source type and record length, faxes oversampled |
| Label agreement | n/a (synthetic) | Two clinicians label a shared subset; report agreement |
| Retrieval scored alone | `retrieval_recall_at_k` from `search()` before generation | Recall@k against gold passages |
| Generation on retrieval successes | `answer_recall_on_retrieval_successes` | Groundedness, correctness, completeness, "not documented" |
| Deterministic checks | Citations must exist; numbers and medications must appear in the cited evidence (brand → ingredient normalization) | Same, with RxNorm normalization through a terminology service |
| Fabricated medications | Counted per statement; rule-of-three bound (0 in 30 statements → ≤ 10% at 95%) | Thresholds such as zero fabricated medications set with clinicians before the pilot |
| LLM judge | Not used | Only after calibration against clinician labels; never the only gate for safety checks |
| Breakdown | Report by stratum, not one number | By site, EHR, source type, rurality |
| Regression | CI runs tests and the eval on every change; `eval --provider ollama --model X` compares models side by side | Full suite on every prompt, chunking, index or model-version change, ready before a retirement notice |

## 6. Risks, and what handles each one

| Risk | What it means for a veteran | Control in this repo | Test / eval case |
|---|---|---|---|
| Fax OCR quality | A misread dose (0.5 mg for 5 mg) quoted silently | Per-page OCR confidence on every chunk; low-confidence text flagged "verify against original page N" in the answer and the evidence card | `test_low_confidence_ocr_is_flagged_not_trusted`, `fax-02` |
| Two EHRs / outside records | Duplicates and conflicting versions of a fact | Provenance on every chunk; outside-only statements tagged "(outside record)" in code; HIE assertions stored as unconfirmed | `meds-01`, `allergy-01` |
| Copied-forward text | A stopped medication looks current | Near-duplicate paragraphs collapse to the earliest copy with its date and a copy count | `test_copied_forward_text_collapses_to_earliest_copy`, `copy-01` |
| Retracted notes | Summary cites something clinically withdrawn | MDM^T11 marks the note entered-in-error; it leaves the index on commit | `test_retracted_note_leaves_the_index` |
| Prompt injection in faxes | The model follows instructions planted in a document | Instruction-like evidence quarantined; evidence marked as data in the prompt; the model can't take actions | `allergy-01` |
| Model version changes | New behavior on the provider's schedule | Safety checks in code, not in the model; model version in every audit record; side-by-side eval per model | Ollama results in README |
| Cost creep | Indexing and token cost grow faster than value | Index on demand; small evidence sets; extractive fallback | — |

## 7. Production pattern in Azure Government (designed, not built here)

- **Network:** App Service or Container Apps with VNet integration; AI Search, Azure OpenAI,
  storage and Key Vault reached through private endpoints with public network access
  disabled. Link the matching private DNS zones to the VNet, or names resolve publicly and
  calls fail. Government endpoints use `.azure.us` / `.usgovcloudapi.net`, so commercial
  templates and DNS zone names don't carry over unchanged.
- **Identity:** managed identities and role-based access, with keys disabled on Azure OpenAI and AI
  Search. The app gets *Cognitive Services OpenAI User* and *Search Index Data Reader*; only
  the ingestion job gets *Search Index Data Contributor*. `AzureOpenAIProvider` takes a token
  provider for the Government audience (`https://cognitiveservices.azure.us/.default`).
- **Deployment:** Terraform (Government environment) or Bicep, one module per component,
  through a pipeline with a reviewed plan; Azure Policy denies public network access and
  unapproved regions.
- **Logs:** prompts and outputs are logged for traceability into an audit store treated as a PHI
  system (in boundary, encrypted, restricted, retention defined); application logs stay
  PHI-free. That's what this repo does: AuditEvents hold the query, logs hold IDs only.
- **Availability:** confirm the model and quota exist in both primary and failover regions
  before promising failover; degrade to plain search if generation is unavailable (the
  extractive fallback here).
- **Platform currency:** model availability and versions in Azure Government and GovCloud change
  frequently. Confirm against the Government model catalog and the FedRAMP Marketplace on the
  day of the decision rather than from memory.

## 8. Verify

**Before the pilot:** a clinician-labeled test set with cases for every risk above, and
thresholds agreed in advance. For example: zero fabricated medications, at least 95% of
statements supported by their citation, and "not documented" instead of a guess.

**During the pilot:** time to find information versus manual chart review, clinician-flagged
errors, weekly use, sampled accuracy review, p95 latency and cost per answered question, all
broken out by site and source type.

**The decision:** expand only where the thresholds held for every site and source type, not
just on average.
