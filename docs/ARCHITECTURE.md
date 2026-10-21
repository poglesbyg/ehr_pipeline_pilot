# Architecture, hosting & authorization brief

*Pilot: VistA → FHIR → grounded clinical search and summarization. Written as the kind of
vendor-neutral recommendation an advisor would bring to a VA program office. Everything
below that depends on a cloud provider's current authorization status is marked
**verify**. Those facts change, so check them against the FedRAMP Marketplace and the
VA TRM on the day of the decision.*

---

## 1. The problem this pilot answers

Clinical search and summarization only works if three things are true at the same time:

1. **The data arrives normalized.** VistA still speaks HL7 v2 over MLLP. The Federal EHR
   (Oracle Health) exposes FHIR R4. Community care arrives as C-CDA through HIE, or as faxes.
   A model can't reason across four formats, so everything is converted to one: **FHIR R4 / US Core**.
2. **Every claim is traceable.** A clinician has to be able to click any sentence and see the
   source. Here that source goes past "the record" all the way down to
   `VISTA LAB · 999-LAB-0042 · OBX[7]`.
3. **The system is auditable and fails safe.** That means patient-scoped retrieval, quarantining
   untrusted text, verifying output before display, a deterministic fallback when the model
   endpoint is down, and an AuditEvent for every question asked.

The pilot shows all three working end to end on synthetic data, with no third-party
dependencies, so it can run inside any boundary.

## 2. Reference architecture

```
                       ┌──────────────────────── Authorization boundary (VA cloud tenant) ────────────────────────┐
 VistA (HL7 v2/MLLP) ──┤► MLLP listener ─┐                                                                         │
 Oracle Health (FHIR) ─┤► FHIR ingest  ──┼─► Pipeline ─► FHIR store ─► Evidence index ─► Assistant ─► Clinician UI │
 HIE C-CDA (pull) ─────┤► C-CDA→FHIR ────┤   parse        US Core      chunk + lineage    retrieve      citations   │
 Community fax ────────┤► OCR (DocIntel)─┘   dedupe       versioned    patient-scoped     screen        lineage     │
                       │                     map          Provenance   hybrid search      generate      audit       │
                       │                     validate                                     verify                    │
                       │                     ACK/NAK                       │                  │                     │
                       │                                                   └── AuditEvent ◄───┘                     │
                       │                                     LLM endpoint (Azure OpenAI / Bedrock, private endpoint)│
                       └────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Pilot component (this repo) | Production equivalent | Why the swap is low-risk |
|---|---|---|
| `hl7bridge.hl7v2` / `mapper` | Same code, or an integration engine (e.g. Rhapsody, InterSystems HealthShare, Azure Health Data Services `$convert-data`) | Mapping contract is the golden test suite; any engine has to pass it |
| `store.FhirStore` (SQLite) | Managed FHIR service (Azure Health Data Services FHIR / AWS HealthLake / HAPI on AKS/EKS) | Store speaks standard FHIR transactions, conditional update and FHIRPath Patch |
| `ai.index.EvidenceIndex` (BM25) | Azure AI Search (hybrid BM25 + vector + semantic ranker) or OpenSearch | Same chunk schema; the patient filter becomes a mandatory security filter |
| `ai.providers.ExtractiveProvider` | Stays as the always-available fallback and the eval baseline | Hosted models must beat it on the gold set |
| `ai.providers.AzureOpenAIProvider` | Azure OpenAI in Azure Government via private endpoint, Entra ID auth | Already implemented; config only |

## 3. Hosting options (vendor-neutral comparison)

| | **A. Azure Government** | **B. AWS GovCloud (US)** | **C. FedRAMP-authorized SaaS** |
|---|---|---|---|
| Fit with existing VA boundaries | Strong. VA Enterprise Cloud has a large Azure Gov footprint, so the platform contractor can inherit controls | Strong. VAEC also runs AWS GovCloud | Depends on the vendor's own ATO plus a VA interconnection agreement |
| LLM service | Azure OpenAI (**verify** model availability and FedRAMP High / IL scope for the specific model) | Amazon Bedrock (**verify** model availability in GovCloud) | Vendor's model; data-use terms must prohibit training on VA data |
| Search | Azure AI Search (**verify** semantic ranker availability in Gov regions) | OpenSearch Service | Bundled |
| FHIR service | Azure Health Data Services / Azure API for FHIR (**verify** region) | HealthLake (**verify** GovCloud availability) or self-managed HAPI | Bundled or none |
| OCR for faxes | Azure AI Document Intelligence | Amazon Textract | Bundled |
| Authorization path | Inherit the CSP's FedRAMP High P-ATO plus VAEC's boundary, then extend the system ATO | Same pattern | Vendor's FedRAMP authorization, VA agency ATO, ISA/MOU |
| Lock-in risk | Moderate (provider abstraction in this repo mitigates it) | Moderate | High: prompts, eval and data flow sit inside the vendor |
| Time to first value | Medium | Medium | Fast, *if* the vendor is already authorized |

**Recommendation pattern:** build the *integration and trust layer* in-house on whichever
VAEC cloud already hosts the data, because that's where VA's differentiation and risk live.
Buy the *commodity services* (LLM, search, OCR) from the CSP inside the same boundary. Only
take full SaaS if a vendor clears the acceptance criteria in §6 on VA's own gold set, *and*
already holds an authorization VA can leverage.

## 4. Authorization boundary & control inheritance

What's **inside** the system boundary: the pipeline, FHIR store, index, assistant API, UI,
and the audit store.
What's **inherited** from the CSP / VAEC: physical security (PE), most of SC-7 at the
network edge, the media protection (MP) family, CSP-managed key infrastructure, and
platform contingency (CP).
**Interconnections** (each needs an ISA/MOU): the VistA HL7 links, the Oracle Health FHIR
endpoints, HIE gateways, and the fax/OCR intake.

### NIST SP 800-53 Rev. 5 controls this design addresses directly

| Control | How the pilot implements it | Production hardening |
|---|---|---|
| **AC-3** Access enforcement | Retrieval is filtered to one patient's identity before scoring (`EvidenceIndex.search`), so cross-patient evidence can't enter a prompt | Mandatory security filter in AI Search; SMART on FHIR patient/user scopes |
| **AC-4** Information flow | Untrusted outside text is quarantined before it reaches the model (`guardrails.screen_evidence`) | Add a classifier-based prompt-shield service in front |
| **AU-2 / AU-3 / AU-12** Audit | `AuditEvent` per question: who asked, which patient, which resources were disclosed, which model answered. `Provenance` per inbound message | Ship to the VA SOC SIEM; immutable storage |
| **AU-10** Non-repudiation | Every resource carries `meta.source` back to an MSH-10 control ID and segment | Sign the ledger entries |
| **IA-2 / IA-5** | *Not in pilot* (localhost only) | VA SSOi / Entra ID, PIV-backed; managed identity to the model endpoint, no keys |
| **SC-8 / SC-13** Transmission | *Pilot:* MLLP in clear on localhost | MLLP over TLS 1.2+ or site-to-site VPN; private endpoints for all PaaS |
| **SC-28** Data at rest | SSN dropped at the boundary by default; ledger stores a redacted copy | CSP encryption with VA-managed keys (CMK) |
| **SI-10** Input validation | C-CDA documents with DTD/ENTITY declarations rejected (no XXE / entity expansion). Strict HL7 parsing, table validation (0001/0004/0357), pre-commit US Core validation; a malformed message returns AE/AR and nothing partial is written | Add the official HL7 FHIR Validator in CI |
| **SI-4 / SI-7** Monitoring & integrity | Duplicate detection on MSH-10; atomic transactions | Alerting on NAK rate and groundedness drift |
| **PM-25 / PT-2** Minimum necessary PII | `ssn_policy=omit`; redacted ledger; logs carry no message content (`log_message` strips query strings) | Privacy Threshold Analysis / PIA |
| **SA-11** Developer testing | 67 unit/integration tests on Python 3.10–3.13 in CI; evaluation harness runs as a release gate | Add the official FHIR Validator and a live-model eval stage |

## 5. Trustworthy AI

These are mapped to the principles in VA's Trustworthy AI Framework. Confirm the current
wording against the published framework before you cite it.

| Principle | Mechanism in this pilot |
|---|---|
| **Purposeful** | Narrow task: summarize and answer from *this patient's record*. Answers describe findings, never prescribe (directive language is flagged) |
| **Effective & safe** | Every sentence is verified against its cited evidence, including number matching. Unsupported sentences are removed before display. The eval gates on groundedness, recall and abstention |
| **Secure & private** | Patient-scoped retrieval, SSN minimization, redacted storage, quarantine for prompt injection, no PHI in logs |
| **Fair & equitable** | Same pipeline for every Veteran; outside (community-care) records are included rather than dropped, but labeled as OCR so they're weighed appropriately. *Next:* stratify eval results by demographics and care setting |
| **Transparent & explainable** | Click any citation to see the original HL7 segment and the FHIR resource; OCR'd evidence is visibly marked; the model and latency are shown on every answer |
| **Accountable & monitored** | AuditEvent per query, Provenance per message, a replayable message ledger, an eval report per release |

## 6. Recommended acceptance criteria for any solution (build or buy)

Measure on VA's own de-identified gold set. Run `python -m hl7bridge eval` to get the format.

| Metric | Threshold to pilot | Threshold to scale |
|---|---|---|
| Groundedness (sentences supported by cited evidence) | ≥ 0.95 | ≥ 0.98 |
| Evidence recall on gold questions | ≥ 0.90 | ≥ 0.95 |
| Abstention on unanswerable questions | ≥ 0.95 | ≥ 0.99 |
| Prompt-injection cases contained | 100% | 100% |
| PHI leakage cases | 0 | 0 |
| p95 answer latency | ≤ 8 s | ≤ 5 s |
| Citation click-through to source | Required | Required |
| Works with model endpoint down (degraded mode) | Required | Required |
| Clinician usefulness (SUS / task-time study) | Baseline captured | ≥ 20% task-time reduction |

## 7. Integration roadmap

| Source | Format / transport | Pilot status | Next step |
|---|---|---|---|
| VistA ADT, Lab, TIU | HL7 v2.x over MLLP | **Implemented**: ADT A01/A02/A03/A04/A08/A11/A40, ORU^R01, MDM^T02/T04 | Pharmacy (RDE/RDS), orders (ORM/OMG), problem list |
| Oracle Health (Federal EHR) | FHIR R4 REST | Store accepts FHIR transactions (`POST /fhir`) | Bulk `$export` into the same store; reconcile identities on ICN |
| HIE (eHealth Exchange / Carequality) | C-CDA via IHE XCA | **Implemented** for CCD allergies, problems, medications and results (`POST /hie/ccda`); outside assertions marked `unconfirmed` | Notes, procedures, immunizations, vitals sections; XCA query/retrieve client; reconciliation workflow |
| Faxed community records | Image/PDF → OCR | Simulated (MDM with OCR text and confidence) | Document Intelligence/Textract; route low-confidence pages to human review |
| CDW | SQL | Not in scope | Use for eval-set construction and population-level validation only |

## 8. Known pilot limitations

These are stated deliberately, because an honest pilot is easier to authorize.

- Naming-system URIs (DFN, local code systems, the ZEL extension) are placeholders, pending the VA naming-system registry.
- `ZEL` is a pilot-defined Z-segment that shows the extension pattern. It isn't VistA's actual layout.
- The pre-commit validator covers the US Core rules this pilot relies on. The official FHIR Validator should run in CI.
- BM25 with a small synonym table stands in for hybrid vector search. It's adequate for the demo, but it isn't production recall.
- The gold set is 14 cases on one synthetic patient, so it proves the *harness*. It is not evidence of clinical performance.
- The Azure OpenAI provider is tested against a mock of the chat-completions contract, not yet against a live deployment.
- C-CDA patient matching uses the ICN when the partner returns it; real-world correlation goes through the MPI.
- No authentication. Localhost binding only.
