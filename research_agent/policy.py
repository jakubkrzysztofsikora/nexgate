"""Fail-closed structural approval gates; human source judgments remain host-owned."""

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from .models import (
    AssertionEvidenceBinding, ClusterResearchDraft, ClusterResearchDraftRequest,
    EvidenceAuthority, ProductPolicy, ResearchEvidenceRecord,
)

LANE_STATUS = {
    "claim_refuted": "false",
    "claim_misleading": "misleading",
    "propagation_only": "unverified",
    "hostile_incident": "not_applicable",
}
PUBLIC_FIELDS = {
    "claim_identity": "claim_text", "shared_narrative": "shared_narrative",
    "publication_sequence": "publication_sequence", "truth_status": "truth_status",
    "verdict_explanation": "verdict_explanation", "verified_context": "verified_context",
    "limitations": "limitations",
}
# A conservative lexical tripwire, not a substitute for independent editorial review.
_PROHIBITED = re.compile(
    r"\b(coordina\w*|koordyn\w*|intent\w*|intend\w*|zamiar\w*|intencj\w*|"
    r"funding|funded|financ\w*|finans\w*|beneficiar\w*|beneficjen\w*|ownership|owned|wlasnosc\w*|"
    r"criminal\w*|przestep\w*|state[- ]sponsor\w*|state[- ]link\w*|"
    r"sponsor\w*|incentiv\w*|motyw\w*)\b"
)


class PolicyViolation(ValueError):
    """An untrusted request/draft cannot cross this policy boundary."""


def canonical_bytes(value: BaseModel) -> bytes:
    """Deterministic UTF-8 encoding used for ceilings and host record commitments."""
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def normalized_claim(text: str) -> str:
    """Conservative identity: NFC and whitespace only, never semantic widening."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def proposition_digest(text: str) -> str:
    return hashlib.sha256(normalized_claim(text).encode("utf-8")).hexdigest()


def prohibited_adverse_claim(text: str) -> bool:
    folded = unicodedata.normalize("NFKD", text.casefold()).replace("\u0142", "l")
    folded = "".join(char for char in folded
                     if not unicodedata.combining(char) and unicodedata.category(char) != "Cf")
    return bool(_PROHIBITED.search(folded))


def _unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise PolicyViolation(f"duplicate {label}")


def validate_request(request: ClusterResearchDraftRequest, policy: ProductPolicy) -> None:
    # Frozen Pydantic objects still contain mutable lists; validate again at each gate.
    request = ClusterResearchDraftRequest.model_validate(request.model_dump())
    policy = ProductPolicy.model_validate(policy.model_dump())
    if len(canonical_bytes(request)) > policy.max_request_bytes:
        raise PolicyViolation("request exceeds canonical byte ceiling")
    if request.product_id != policy.product_id or request.task not in policy.allowed_tasks:
        raise PolicyViolation("product/task is not authorized")
    if request.claim_id != policy.reviewed_claim_id or request.proposition_digest != proposition_digest(policy.reviewed_claim_text):
        raise PolicyViolation("request claim identity does not match reviewed authority")
    if request.member_coverage.coverage_rule_version != "cluster-member-stance-v1":
        raise PolicyViolation("unknown coverage authority")
    if not request.member_coverage.eligible_member_count:
        raise PolicyViolation("empty eligible member coverage")
    if len(request.representatives) > request.member_coverage.eligible_member_count:
        raise PolicyViolation("representative count exceeds eligible coverage")
    _unique(request.authority_evidence_ids, "authority evidence")
    _unique([row.evidence_id for row in request.representatives], "representative evidence")
    _unique([row.evidence_id for row in policy.evidence_authorities], "host authority evidence")
    for row in request.comment_aggregates:
        if (row.parent_post_count < policy.minimum_parent_posts
                or row.outlet_count < policy.minimum_outlets
                or row.distinct_author_lower_bound < policy.minimum_authors):
            raise PolicyViolation("comment aggregate below privacy threshold")
        if row.timestamp_precision == "exact":
            raise PolicyViolation("comment timing must remain interval-censored")
        if (row.minimum_gap_lower_bound_seconds is not None
                and row.minimum_gap_lower_bound_seconds == row.minimum_gap_upper_bound_seconds):
            raise PolicyViolation("comment timing interval must not disclose exact gap")


def validate_draft(
    draft: ClusterResearchDraft, request: ClusterResearchDraftRequest,
    evidence_records: Mapping[str, ResearchEvidenceRecord], policy: ProductPolicy,
) -> None:
    validate_request(request, policy)
    draft = ClusterResearchDraft.model_validate(draft.model_dump())
    if len(canonical_bytes(draft)) > policy.max_result_bytes:
        raise PolicyViolation("result exceeds canonical byte ceiling")
    if draft.request_id != request.request_id:
        raise PolicyViolation("draft request_id mismatch")
    if draft.truth_status != LANE_STATUS[request.evidence_basis]:
        raise PolicyViolation("truth_status conflicts with evidence_basis")
    if draft.truth_status in {"false", "misleading"}:
        if request.member_coverage.mixed_polarity_veto:
            raise PolicyViolation("mixed polarity veto")
        if request.member_coverage.support_ratio < max(request.member_coverage.minimum_support_ratio, policy.minimum_support_ratio):
            raise PolicyViolation("support ratio below reviewed threshold")
    if normalized_claim(draft.claim_text) != normalized_claim(policy.reviewed_claim_text):
        raise PolicyViolation("draft claim identity widens or changes reviewed proposition")
    if len(evidence_records) > 256:
        raise PolicyViolation("too many evidence records")
    for key, record in evidence_records.items():
        record = ResearchEvidenceRecord.model_validate(record.model_dump())
        if key != record.evidence_id:
            raise PolicyViolation("evidence record key mismatch")
    known = set(evidence_records)
    if not set(request.authority_evidence_ids) <= known:
        raise PolicyViolation("unknown evidence in request authority")
    _unique(draft.claim_member_evidence_ids, "claim member evidence")
    representatives = {row.evidence_id: row for row in request.representatives}
    if not set(draft.claim_member_evidence_ids) <= known or not set(draft.claim_member_evidence_ids) <= set(representatives):
        raise PolicyViolation("unknown evidence in claim member identity")
    if draft.representation_type == "exact_quote" and not any(
        draft.claim_text in representatives[evidence_id].excerpt
        for evidence_id in draft.claim_member_evidence_ids
    ):
        raise PolicyViolation("exact quote absent from cited representative")
    _unique([row.assertion_id for row in draft.assertions], "assertion ID")
    for assertion in draft.assertions:
        _unique(assertion.evidence_ids, "assertion evidence")
        if not set(assertion.evidence_ids) <= known:
            raise PolicyViolation(f"unknown evidence for {assertion.assertion_id}")
    prose = [draft.claim_text, draft.shared_narrative, draft.publication_sequence,
             draft.verdict_explanation or "", draft.verified_context or "", draft.limitations,
             *(row.proposition for row in draft.assertions)]
    if any(prohibited_adverse_claim(text) for text in prose):
        raise PolicyViolation("P0 prohibited assertion class")


def assert_public_fields_are_fully_reconstructed(draft: ClusterResearchDraft, request: ClusterResearchDraftRequest) -> None:
    for assertion_class, field in PUBLIC_FIELDS.items():
        rows = [row.proposition for row in draft.assertions if row.assertion_class == assertion_class]
        if assertion_class in {"claim_identity", "truth_status"} and len(rows) != 1:
            raise PolicyViolation(f"unrepresented public prose: {field} requires one atomic assertion")
        expected = " ".join(rows) if rows else None
        if getattr(draft, field) != expected:
            raise PolicyViolation(f"unrepresented public prose: {field}")
    # Coverage has a structured public projection, so it uses the exact canonical object.
    for row in draft.assertions:
        if row.assertion_class == "member_coverage" and row.proposition != canonical_bytes(request.member_coverage).decode():
            raise PolicyViolation("unrepresented public prose: member_coverage")


def _verified_authorities(request, evidence_records, policy) -> dict[str, EvidenceAuthority]:
    authorities = {row.evidence_id: row for row in policy.evidence_authorities}
    needed = set(request.authority_evidence_ids)
    for evidence_id in needed:
        if evidence_id not in authorities:
            raise PolicyViolation("missing host source authority")
    for evidence_id, authority in authorities.items():
        if evidence_id not in evidence_records:
            continue
        record = evidence_records[evidence_id]
        if (not authority.capture_verified or not authority.current
                or authority.claim_id != request.claim_id
                or authority.proposition_digest != request.proposition_digest
                or authority.record_sha256 != hashlib.sha256(canonical_bytes(record)).hexdigest()):
            raise PolicyViolation("unverified, stale, or mismatched evidence authority")
        if authority.unresolved_conflict:
            raise PolicyViolation("source conflict requires editorial escalation")
    return authorities


def _enforce_lane_authority(request, matching, evidence_records, authorities) -> None:
    ids = {row.evidence_id for row in matching} & set(request.authority_evidence_ids)
    rows = [(evidence_records[eid], authorities[eid]) for eid in ids]
    lane = request.evidence_basis
    if lane == "propagation_only":
        if not any(a.complete_member_coverage for _, a in rows) or not any(a.signed_publication_sequence for _, a in rows):
            raise PolicyViolation("propagation authority requires complete coverage and signed sequence")
        return
    if lane == "hostile_incident":
        if not any(a.reviewed_incident for _, a in rows):
            raise PolicyViolation("incident authority requires preserved reviewed artifacts")
        return
    usable = [(r, a) for r, a in rows if not a.interested_party and a.reliable]
    if lane == "claim_refuted" and any(r.source_class == "claimreview" and a.qualifying_claimreview for r, a in usable):
        return
    primaries = [r for r, a in usable if r.source_class in {"primary", "official"} and a.authoritative]
    if not any(primary.independence_group != other.independence_group
               for primary in primaries for other, _ in usable
               if other.source_class in {"primary", "official", "independent_reporting", "claimreview"}):
        raise PolicyViolation("lane authority requires authoritative primary and independent reliable corroboration")


def validate_approved_assertions(
    draft: ClusterResearchDraft, bindings: Sequence[AssertionEvidenceBinding],
    request: ClusterResearchDraftRequest, evidence_records: Mapping[str, ResearchEvidenceRecord],
    policy: ProductPolicy,
) -> None:
    """Validate a host-reviewed revision immediately before constructing its snapshot.

    A truth_status/contradicts decision refers to the immutable reviewed claim,
    whose exact identity and source attestations are checked here. This routine
    neither fetches archives nor judges entailment; the host must recompute its
    attestations from current verified captures and independent review decisions.
    """
    validate_draft(draft, request, evidence_records, policy)
    assert_public_fields_are_fully_reconstructed(draft, request)
    if len(bindings) > 16_384:
        raise PolicyViolation("too many assertion bindings")
    assertions = {row.assertion_id: row for row in draft.assertions}
    seen = set()
    for raw in bindings:
        row = AssertionEvidenceBinding.model_validate(raw.model_dump())
        pair = (row.assertion_id, row.evidence_id)
        if pair in seen:
            raise PolicyViolation("duplicate assertion binding")
        seen.add(pair)
        if (row.assertion_id not in assertions or row.evidence_id not in evidence_records
                or row.evidence_id not in assertions[row.assertion_id].evidence_ids):
            raise PolicyViolation("orphan assertion binding")
    authorities = _verified_authorities(request, evidence_records, policy)
    for assertion in assertions.values():
        relation = "contradicts" if assertion.assertion_class == "truth_status" and request.evidence_basis == "claim_refuted" else "supports"
        matching = [row for row in bindings if row.assertion_id == assertion.assertion_id
                    and row.reviewer_decision == "accepted" and row.relation == relation]
        if not matching:
            raise PolicyViolation(f"required relation {relation} missing for {assertion.assertion_id}")
        for evidence_id in assertion.evidence_ids:
            if (assertion.assertion_id, evidence_id) not in seen:
                raise PolicyViolation("missing evidence binding decision")
            if evidence_id not in authorities:
                raise PolicyViolation("missing host source authority")
        if assertion.assertion_class in {"truth_status", "verdict_explanation"}:
            _enforce_lane_authority(request, matching, evidence_records, authorities)
    claim_identity = next(row for row in assertions.values() if row.assertion_class == "claim_identity")
    accepted_members = {row.evidence_id for row in bindings
                        if row.assertion_id == claim_identity.assertion_id
                        and row.reviewer_decision == "accepted" and row.relation == "supports"}
    for evidence_id in draft.claim_member_evidence_ids:
        if evidence_id not in authorities:
            raise PolicyViolation("missing claim-member host source authority")
        if evidence_id not in accepted_members:
            raise PolicyViolation("claim-member citation requires accepted claim_identity supports binding")
