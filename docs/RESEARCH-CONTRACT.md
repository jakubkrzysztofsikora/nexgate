# Research draft contract

`research_agent.models` defines the version-one request, draft, assertion,
coverage, representative, comment-aggregate, evidence-record, and binding wire
objects. Unknown fields are rejected, fields are bounded, and instances are
frozen. Lists remain JSON-compatible lists; gates revalidate them rather than
relying on shallow freezing. The tested runtime is the Task 3 image with
LiteLLM 1.100.1, a2a-sdk 1.1.2, and Pydantic 2.13.5.

## Host policy and evidence ownership

`ProductPolicy` and `EvidenceAuthority` are trusted host inputs, never generated
draft fields. `validate_request(request, policy)` checks complete member
coverage, aggregate privacy thresholds, reviewed claim identity, and the
canonical request byte ceiling. `validate_draft(draft, request, records, policy)`
also checks the immutable lane/status mapping, mixed-polarity veto, support
threshold, representative claim references, known evidence IDs, result size,
and the P0 prohibited-language tripwire.

The host must build `ProductPolicy.reviewed_claim_id` and `reviewed_claim_text`
from its reviewed claim authority. The proposition digest is SHA-256 over
UTF-8 NFC text with whitespace collapsed (`policy.proposition_digest`). Draft
claim identity uses that same conservative normalization. Paraphrase/synthesis
labels do not permit the model to replace the reviewed proposition. Exact
quotes must additionally occur in a cited representative excerpt.

For each cited evidence ID, the host supplies an `EvidenceAuthority`:

- `claim_id` and `proposition_digest`: the exact reviewed proposition.
- `record_sha256`: SHA-256 of `policy.canonical_bytes(record)`, binding the entire
  verified record, including passage, archive reference, and independence group.
- `capture_verified` and `current`: archive byte/text verification and current
  eligibility, recomputed at approval rather than copied from worker claims.
- `authoritative`, `reliable`, `qualifying_claimreview`, `interested_party`, and
  `unresolved_conflict`: host/editorial source determinations. Qualifying
  ClaimReview includes exact proposition, publisher, and rating verification.
- `complete_member_coverage`, `signed_publication_sequence`, and
  `reviewed_incident`: provenance for the respective non-adverse evidence lanes.

Only the trusted caller can establish these facts. The research producer verifies
archive bytes; it does not inspect signatures, determine retention eligibility,
or infer source reliability. Lustro must persist those checks and recompute attestations under
its publication transaction. An arbitrary source-class label is insufficient.
Evidence records use `editorial-research/sha256/<content_sha256>` archive keys.

## Atomic approval and public reconstruction

`validate_approved_assertions(draft, bindings, request, records, policy)` runs
the draft gate again, reconstructs public fields, verifies host attestations,
and checks every evidence-binding decision. Each public text field is exactly
the space-joined propositions for its assertion class, in assertion order.
`claim_identity` maps to `claim_text`; the other classes match field names.
Claim identity and truth status each require exactly one assertion. Null
optional fields require no assertions. If a `member_coverage` assertion is
provided, its proposition must be the canonical coverage JSON; coverage itself
comes from the immutable request.

Every cited pair needs one accepted or rejected review decision. Duplicate,
orphan, uncited, or unknown bindings fail. Each assertion needs an accepted
`supports` binding, except `truth_status` in `claim_refuted`, which requires
`contradicts`. That contradiction is against the immutable reviewed claim
identity, not against the literal status word `false`. Rejected and
`context_only` decisions cannot satisfy the required relation.

Every `claim_member_evidence_ids` entry additionally needs a verified, current
host attestation and an accepted `supports` binding on the `claim_identity`
assertion. A quote shown in a representative excerpt cannot bypass this check
by binding claim identity only to a separate fact-check record.

The lane authority rule runs for `truth_status` and `verdict_explanation`
assertions' qualifying bindings and uses only IDs in the request's reviewed
authority set. Descriptive assertions (claim identity, member coverage, shared
narrative, publication sequence, verified context, and limitations) require
verified accepted support; their sources need not establish the verdict.
This clarifies the brief's over-broad per-assertion pseudocode in favor of the
design's distinction between descriptive support and adverse verdict authority.
Human review must reject an adverse verdict disguised as a descriptive class.

A false verdict requires
a qualifying ClaimReview or authoritative primary/official evidence plus
reliable corroboration in a distinct independence group. Misleading requires
the latter combination. Interested sources do not establish adverse verdicts.
Propagation requires complete coverage and signed sequence authority; incidents
require preserved reviewed artifacts. Stale, unverified, changed, and
conflicting source attestations fail closed.

For a false claim, `truth_status` requires the accepted contradiction of the
reviewed claim. `verdict_explanation` needs accepted support for its explanatory
prose plus the lane's authority; it does not require a contradiction of the
explanation itself. The draft always retains its separately validated status
assertion. Misleading status and its explanation require accepted support and
authoritative context plus independent corroboration for the same proposition.

The lexical P0 guard conservatively blocks common English/Polish coordination,
intent, funding, beneficiary, ownership, criminality, state-sponsorship, and
incentive terminology, including some Unicode obfuscation. It is not a semantic
classifier or proof that arbitrary prose contains no prohibited implication.
Independent editorial review must establish atomicity, entailment, prohibited
claim absence, and requester/reviewer separation before bindings are accepted.
Publication snapshot locking, archive re-verification, immutable revisions,
and signature/content-address generation belong to the later host integration.

## Bounds and verification

Defaults: 128 KiB canonical UTF-8 request/draft ceilings, 64 representatives,
64 aggregates, 128 assertions, 128 IDs per assertion, 256 evidence records,
and 16,384 binding decisions. Text limits are 200-character IDs, 500-character
labels, 8,000-character propositions, 16,000-character passages, 2,048-character
URLs, and eight redirects. Policy cannot lower privacy thresholds below three
parents, three outlets, and five distinct authors. Exact comment timing and
zero-width gap intervals are rejected.

Run `python3 -m pytest -q tests/research_agent` and
`python3 -m compileall -q research_agent` in an environment with Pydantic v2 and
pytest. `./scripts/validate.sh` includes the contract tests in offline validation.
The pinned runtime image already supplies Pydantic; no runtime/config change
is required for this contract. Task 4's report records the read-only pytest
mounts used to exercise that unchanged image without installing packages.

## Bounded capture and research adapters

`await research_cluster(request, policy, search, model, evidence_store)` returns
a `ResearchRun` containing the strict draft, host-built evidence records, and a
host-built usage manifest. It does not approve or publish the draft. Supply
`TavilySearch()` and `LiteLLMModel()` explicitly to use real services. Search uses
`TAVILY_API_KEY`; the model uses `LITELLM_BASE_URL`, `LITELLM_API_KEY`, and
`RESEARCH_MODEL_ALIAS` (default `gpt-5.6-sol`). No routing configuration changes.
The existing LiteLLM runtime supplies HTTPX and Pydantic v2; local tests need
those packages alongside pytest. Network calls transmit the request and captured
passages to the configured model, and planned queries to Tavily.

The trusted caller supplies `EvidenceStore(backend, seed_records=...)`. Seeds
include every request authority ID and all representative records that may be
cited. They are host inputs, never model output. The asynchronous backend
implements `put(key, bytes)` and `get(key, byte_limit) -> bytes`;
`S3Backend(configured_s3_client, private_bucket)` adapts an existing boto-style
private S3-compatible client. The caller configures SDK deadlines and retries.
The library does not create buckets or discover credentials. Uploads use
`editorial-research/sha256/<digest>` and read-after-write SHA-256 verification;
all returned archives, including seeds, are verified again after synthesis.
Deleted, missing, corrupt, or unwritable archives fail the run.

Capture connects only to an explicitly validated global address. DNS results are
validated together, the selected address is pinned in the HTTPX URL, and the
original hostname is used for Host and verified TLS SNI. Connection reuse is
disabled to prevent cross-host TLS reuse when redirect hosts share an IP.
Every redirect is revalidated and re-resolved. The P0 implementation is more
restrictive than the allowed-port ceiling: HTTPS/443 only, no downgrade to
HTTP/80. It also accepts identity encoding only, so compressed and decoded byte
ceilings coincide. HTML, XHTML, and plain text are accepted; scripts, styles,
and templates are excluded from extracted HTML text. No page commands execute.
`trust_env=False` prevents environment proxies for source and service clients.
Service URLs are trusted host configuration and are separate from source URLs.

`CaptureEnvelope` retains raw bytes, text, both hashes, exact passage, retrieval
time, redirect chain, and pinned addresses. New records get producer-local
`src:<sha256>` IDs derived from final URL and content/text hashes. They are
`commentary` in one shared `unreviewed:all` independence group, without author
or publication-date claims. Lustro must recompute its canonical evidence IDs,
re-extract and verify the passage, classify sources, review independence and
relations, and remap citations before signing. Capture verification grants no
editorial authority. Seed records preserve their existing host IDs.

Host policy defaults: three iterations, four queries per iteration (primary,
contrary, named-subject/correction, ownership/syndication), sixteen total sources
including seeds, one million source bytes, four redirects, five-second connect,
ten-second read, thirty-second source/API and 300-second run deadlines, 4,000
output tokens per model call, and 256 KiB model data input. Existing canonical
request/result bounds also apply; the result bound covers the complete run.
Each iteration permits one plan call and one synthesis call. Structured schemas
reject extra fields, truncated/tool/refusal outputs fail closed, and untrusted
request/page text stays in a separate data message. The manifest counts host
observed calls and queries rather than accepting model-owned bookkeeping.

Search adapters accept `search(queries, policy, *, remaining_sources,
excluded_urls)`. Orchestration supplies capacity after seeds and previous
captures, plus their original and final URLs. Tavily collects bounded results
for all categories before selecting distinct URLs round-robin. When four slots
and four categories with new distinct candidates remain, every category reaches
capture before any category gets a second slot. Existing captured URLs consume
no new slots; less than four remaining slots cannot cover all categories.

For deterministic host re-extraction, the record's existing `extractor_version`
field now selects the entire algorithm: `plain-utf8-v1` decodes UTF-8 with
replacement and preserves text exactly; `html-text-v1` decodes the same way,
uses `TextExtractor`/`HTMLParser(convert_charrefs=True)`, omits script/style/template
data, strips each nonempty data segment, and joins segments with newline.
HTML and XHTML share those semantics. `extract_text(archived_bytes, version)`
replays either path without needing the original HTTP MIME. Unknown versions
fail closed. SHA-256 is over UTF-8 extracted text; the exact passage is its first
16,000 characters. The version is persisted in the evidence record and included
in producer identity derivation, even if two algorithms happen to return the
same text. Task 6 must dispatch by this version and bind it during verification;
it must not infer the algorithm from the URL or assume all archives are HTML.

Model calls carry `policy.reviewed_claim_id` and `policy.reviewed_claim_text` in
a separate host-reviewed system context. Request hypotheses and fetched pages
remain untrusted user data. Synthesis is instructed to preserve the reviewed
claim even when the hypothesis differs; the host validation gate still enforces
equality. Both trusted context and untrusted data count toward the model input
byte ceiling.

Run `python3 -m pytest -q tests/research_agent tests/integration/test_research_adapters.py`.
Default tests use deterministic transports and loopback API servers without real
credentials. `RESEARCH_LIVE_TESTS=1` explicitly enables the one-query Tavily
credentials/quota probe; it is never enabled by default or by validation scripts.
Live provider quota, model quality, and real private S3 availability are deployment
checks, not assertions established by the deterministic suite.
