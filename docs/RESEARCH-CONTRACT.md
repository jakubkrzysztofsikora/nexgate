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

Only the trusted caller can establish these facts. The library does not fetch
archives, inspect signatures, determine retention eligibility, or infer source
reliability. Lustro must persist those checks and recompute attestations under
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
