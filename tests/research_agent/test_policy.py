import hashlib
import json

import pytest
from pydantic import ValidationError

from research_agent.models import (
    AssertionEvidenceBinding, ClusterResearchDraft, ClusterResearchDraftRequest,
    EvidenceAuthority, ProductPolicy, ResearchEvidenceRecord,
)
from research_agent.policy import PolicyViolation, validate_approved_assertions, validate_draft, validate_request
from test_contract import request_payload


def digest(value):
    return hashlib.sha256(json.dumps(value.model_dump(mode="json"), sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def setup_case(lane="claim_refuted"):
    claim = "The number is 5."
    proposition_digest = hashlib.sha256(claim.encode()).hexdigest()
    request = ClusterResearchDraftRequest.model_validate(request_payload() | {
        "evidence_basis": lane, "proposition_digest": proposition_digest,
    })
    record = ResearchEvidenceRecord.model_validate({
        "evidence_id": "src:1", "url": "https://example.org/record",
        "final_url": "https://example.org/record", "redirect_chain": [],
        "title": "Record", "publisher": "Publisher", "author": None,
        "published_at": None, "updated_at": None, "retrieved_at": "2026-09-11T12:00:00Z",
        "source_class": "claimreview", "independence_group": "publisher:1",
        "exact_passage": "The number is 4.", "passage_locator": "paragraph 1",
        "content_sha256": "b" * 64, "extracted_text_sha256": "c" * 64,
        "extractor_version": "v1", "archive_ref": "editorial-research/sha256/" + "b" * 64,
    })
    authority = EvidenceAuthority(
        evidence_id="src:1", claim_id="claim:1", proposition_digest=proposition_digest,
        record_sha256=digest(record), capture_verified=True, current=True,
        qualifying_claimreview=True, authoritative=True, reliable=True,
        complete_member_coverage=True, signed_publication_sequence=True,
        reviewed_incident=True,
    )
    policy = ProductPolicy(reviewed_claim_id="claim:1", reviewed_claim_text=claim,
                           evidence_authorities=[authority])
    status = {"claim_refuted": "false", "claim_misleading": "misleading",
              "propagation_only": "unverified", "hostile_incident": "not_applicable"}[lane]
    fields = {"claim_identity": claim, "shared_narrative": "Members describe a number.",
              "publication_sequence": "Publication began yesterday.", "truth_status": status,
              "verdict_explanation": "The reviewed record gives another number.",
              "verified_context": "The record gives 4.", "limitations": "Only ten members were eligible."}
    assertions = [{"assertion_id": key, "proposition": value,
                   "assertion_class": key, "evidence_ids": ["src:1"]} for key, value in fields.items()]
    draft = ClusterResearchDraft(request_id=request.request_id, claim_text=claim,
        representation_type="faithful_paraphrase", claim_member_evidence_ids=["src:1"],
        assertions=assertions, **{key: value for key, value in fields.items() if key != "claim_identity"})
    bindings = [AssertionEvidenceBinding(assertion_id=row.assertion_id, evidence_id="src:1",
        relation="contradicts" if lane == "claim_refuted" and row.assertion_class == "truth_status" else "supports",
        reviewer_decision="accepted") for row in draft.assertions]
    return request, draft, {"src:1": record}, policy, bindings


def change(model, **changes):
    return type(model).model_validate(model.model_dump() | changes)


@pytest.mark.parametrize("lane", ["claim_refuted", "propagation_only", "hostile_incident"])
def test_valid_lane_can_be_approved(lane):
    request, draft, records, policy, bindings = setup_case(lane)
    validate_approved_assertions(draft, bindings, request, records, policy)


@pytest.mark.parametrize("lane,status", [
    ("claim_refuted", "unverified"), ("claim_misleading", "false"),
    ("propagation_only", "false"), ("hostile_incident", "misleading"),
])
def test_lane_status_matrix_is_exact(lane, status):
    request, draft, records, policy, _ = setup_case(lane)
    with pytest.raises(PolicyViolation, match="truth_status"):
        validate_draft(change(draft, truth_status=status), request, records, policy)


def test_mixed_polarity_veto_and_support_threshold_block_adverse_verdict():
    request, draft, records, policy, _ = setup_case()
    for coverage in (change(request.member_coverage, mixed_polarity_veto=True),
                     change(request.member_coverage, minimum_support_ratio=0.9)):
        with pytest.raises(PolicyViolation, match="mixed polarity|support ratio"):
            validate_draft(draft, change(request, member_coverage=coverage), records, policy)


def test_draft_cannot_mint_evidence_and_records_must_match_keys():
    request, draft, records, policy, _ = setup_case()
    assertions = [change(draft.assertions[0], evidence_ids=["model:invented"]), *draft.assertions[1:]]
    with pytest.raises(PolicyViolation, match="unknown evidence"):
        validate_draft(change(draft, assertions=assertions), request, records, policy)
    with pytest.raises(PolicyViolation, match="evidence.*key"):
        validate_draft(draft, request, {"wrong": records["src:1"]}, policy)


@pytest.mark.parametrize("field,value", [("manifest", {}), ("evidence_records", []), ("iterations", 1)])
def test_draft_cannot_supply_host_metadata(field, value):
    _, draft, _, _, _ = setup_case()
    with pytest.raises(ValidationError):
        change(draft, **{field: value})


@pytest.mark.parametrize("relation,decision", [("context_only", "accepted"), ("supports", "rejected")])
def test_non_supporting_bindings_cannot_approve_public_prose(relation, decision):
    request, draft, records, policy, bindings = setup_case()
    bindings[0] = change(bindings[0], relation=relation, reviewer_decision=decision)
    with pytest.raises(PolicyViolation, match="required relation"):
        validate_approved_assertions(draft, bindings, request, records, policy)


def test_unrepresented_prose_or_status_and_orphan_bindings_are_rejected():
    request, draft, records, policy, bindings = setup_case()
    with pytest.raises(PolicyViolation, match="unrepresented public prose"):
        validate_approved_assertions(change(draft, verified_context="Extra sentence."), bindings, request, records, policy)
    with pytest.raises(PolicyViolation, match="orphan"):
        validate_approved_assertions(draft, bindings + [change(bindings[0], assertion_id="missing")], request, records, policy)
    with pytest.raises(PolicyViolation, match="duplicate"):
        validate_approved_assertions(draft, bindings + [bindings[0]], request, records, policy)


@pytest.mark.parametrize("flags", [
    {"capture_verified": False}, {"current": False}, {"interested_party": True},
    {"unresolved_conflict": True}, {"qualifying_claimreview": False},
    {"proposition_digest": "e" * 64}, {"record_sha256": "f" * 64},
])
def test_host_authority_cannot_be_replaced_by_source_label(flags):
    request, draft, records, policy, bindings = setup_case()
    policy = change(policy, evidence_authorities=[change(policy.evidence_authorities[0], **flags)])
    with pytest.raises(PolicyViolation):
        validate_approved_assertions(draft, bindings, request, records, policy)


@pytest.mark.parametrize("lane", ["claim_refuted", "claim_misleading"])
def test_primary_authority_needs_independent_reliable_corroboration(lane):
    request, draft, records, policy, bindings = setup_case(lane)
    record = change(records["src:1"], source_class="primary")
    records["src:1"] = record
    authority = change(policy.evidence_authorities[0], record_sha256=digest(record), qualifying_claimreview=False)
    policy = change(policy, evidence_authorities=[authority])
    with pytest.raises(PolicyViolation, match="authority"):
        validate_approved_assertions(draft, bindings, request, records, policy)
    second = change(record, evidence_id="src:2", source_class="independent_reporting", independence_group="publisher:2")
    records["src:2"] = second
    policy = change(policy, evidence_authorities=[authority, change(authority, evidence_id="src:2", record_sha256=digest(second), authoritative=False)])
    request = change(request, authority_evidence_ids=["src:1", "src:2"])
    draft = change(draft, assertions=[change(row, evidence_ids=["src:1", "src:2"]) for row in draft.assertions])
    bindings += [change(row, evidence_id="src:2") for row in bindings]
    validate_approved_assertions(draft, bindings, request, records, policy)
    second = change(second, independence_group="publisher:1")
    records["src:2"] = second
    policy = change(policy, evidence_authorities=[authority, change(policy.evidence_authorities[1], record_sha256=digest(second))])
    with pytest.raises(PolicyViolation, match="authority"):
        validate_approved_assertions(draft, bindings, request, records, policy)


def test_request_identity_and_utf8_byte_ceiling_are_enforced():
    request, draft, records, policy, _ = setup_case()
    with pytest.raises(PolicyViolation, match="identity"):
        validate_request(change(request, proposition_digest="e" * 64), policy)
    with pytest.raises(PolicyViolation, match="claim identity"):
        validate_draft(change(draft, claim_text="A wider unrelated claim."), request, records, policy)
    with pytest.raises(PolicyViolation, match="byte ceiling"):
        validate_request(request, change(policy, max_request_bytes=100))
    with pytest.raises(PolicyViolation, match="byte ceiling"):
        validate_draft(draft, request, records, change(policy, max_result_bytes=100))


def test_small_comment_cells_and_exact_timing_are_rejected():
    request, _, _, policy, _ = setup_case()
    aggregate = dict(normalized_template_count=5, parent_post_count=3, outlet_count=3,
        distinct_author_lower_bound=5, timestamp_precision="hour",
        minimum_gap_lower_bound_seconds=0, minimum_gap_upper_bound_seconds=3600,
        definitely_within_1h_count=2, possibly_within_1h_count=3, unresolved_count=0,
        dedup_method_version="v1", timing_method_version="v1")
    validate_request(change(request, comment_aggregates=[aggregate]), policy)
    for update in ({"parent_post_count": 2, "outlet_count": 2}, {"outlet_count": 2},
                   {"distinct_author_lower_bound": 4}, {"timestamp_precision": "exact"}):
        with pytest.raises(PolicyViolation, match="comment"):
            validate_request(change(request, comment_aggregates=[aggregate | update]), policy)


@pytest.mark.parametrize("text", ["The group coordinated the campaign.", "State sponsorship explains this.",
                                    "Finansowanie pochodzi od partii.", "They intended to mislead."])
def test_prohibited_adverse_propositions_fail_closed(text):
    request, draft, records, policy, _ = setup_case()
    with pytest.raises(PolicyViolation, match="P0 prohibited"):
        validate_draft(change(draft, verified_context=text), request, records, policy)


def test_mutated_frozen_model_lists_are_revalidated_at_gate():
    request, draft, records, policy, _ = setup_case()
    draft.assertions[0].evidence_ids.clear()
    with pytest.raises((PolicyViolation, ValidationError)):
        validate_draft(draft, request, records, policy)


@pytest.mark.parametrize("text", ["The publisher is owned by a party.",
                                    "They coor\u200bdinated the campaign.",
                                    "The campaign was financed by the state."])
def test_prohibited_ownership_financing_and_invisible_characters(text):
    request, draft, records, policy, _ = setup_case()
    with pytest.raises(PolicyViolation, match="P0 prohibited"):
        validate_draft(change(draft, verified_context=text), request, records, policy)


def test_status_requires_contradiction_of_reviewed_claim_not_support():
    request, draft, records, policy, bindings = setup_case()
    bindings = [change(row, relation="supports") if row.assertion_id == "truth_status" else row for row in bindings]
    with pytest.raises(PolicyViolation, match="required relation contradicts"):
        validate_approved_assertions(draft, bindings, request, records, policy)


def test_public_prose_reconstruction_is_ordered_and_complete():
    request, draft, records, policy, bindings = setup_case()
    extra = change(draft.assertions[-1], assertion_id="second_limit", proposition="The archive may be incomplete.")
    draft = change(draft, limitations=draft.limitations + " " + extra.proposition,
                   assertions=[*draft.assertions, extra])
    bindings.append(change(bindings[-1], assertion_id="second_limit"))
    validate_approved_assertions(draft, bindings, request, records, policy)
    with pytest.raises(PolicyViolation, match="unrepresented public prose"):
        validate_approved_assertions(change(draft, assertions=list(reversed(draft.assertions))), bindings, request, records, policy)


def test_every_cited_evidence_requires_review_and_binds_exact_record():
    request, draft, records, policy, bindings = setup_case()
    with pytest.raises(PolicyViolation, match="required relation"):
        validate_approved_assertions(draft, bindings[:-1], request, records, policy)
    changed = {"src:1": change(records["src:1"], exact_passage="Different captured passage.")}
    with pytest.raises(PolicyViolation, match="mismatched evidence authority"):
        validate_approved_assertions(draft, bindings, request, changed, policy)
    with pytest.raises(PolicyViolation, match="missing host source authority"):
        validate_approved_assertions(draft, bindings, request, records, change(policy, evidence_authorities=[]))


def test_request_and_result_ceilings_count_utf8_bytes_at_exact_boundary():
    request, draft, records, policy, _ = setup_case()
    draft = change(draft, verified_context="\u017c" * 100)
    size = len(json.dumps(draft.model_dump(mode="json"), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8"))
    validate_draft(draft, request, records, change(policy, max_result_bytes=size))
    with pytest.raises(PolicyViolation, match="byte ceiling"):
        validate_draft(draft, request, records, change(policy, max_result_bytes=size - 1))


@pytest.mark.parametrize("lane,flag", [("propagation_only", "complete_member_coverage"),
                                      ("propagation_only", "signed_publication_sequence"),
                                      ("hostile_incident", "reviewed_incident")])
def test_non_adverse_lanes_require_their_specific_authority(lane, flag):
    request, draft, records, policy, bindings = setup_case(lane)
    policy = change(policy, evidence_authorities=[change(policy.evidence_authorities[0], **{flag: False})])
    with pytest.raises(PolicyViolation, match="authority"):
        validate_approved_assertions(draft, bindings, request, records, policy)


def test_duplicate_assertions_and_uncited_bindings_cannot_smuggle_approval():
    request, draft, records, policy, bindings = setup_case()
    with pytest.raises(PolicyViolation, match="duplicate assertion"):
        validate_draft(change(draft, assertions=[*draft.assertions, draft.assertions[0]]), request, records, policy)
    with pytest.raises(PolicyViolation, match="orphan"):
        validate_approved_assertions(draft, [*bindings, change(bindings[0], evidence_id="model:invented")], request, records, policy)


def test_exact_quote_and_request_correlation_cannot_be_forged():
    request, draft, records, policy, _ = setup_case()
    with pytest.raises(PolicyViolation, match="exact quote"):
        validate_draft(change(draft, representation_type="exact_quote"), request, records, policy)
    with pytest.raises(PolicyViolation, match="request_id"):
        validate_draft(change(draft, request_id=request.cluster_id), request, records, policy)


def separate_sources_case(lane="claim_refuted"):
    request, draft, records, policy, bindings = setup_case(lane)
    member = change(records["src:1"], evidence_id="member:1", source_class="commentary",
                    independence_group="member:publisher", exact_passage=draft.claim_text)
    chronology = change(records["src:1"], evidence_id="sequence:1", source_class="primary",
                        independence_group="sequence:publisher",
                        exact_passage="Ten eligible members; the first publication was yesterday.")
    records.update({member.evidence_id: member, chronology.evidence_id: chronology})
    policy = change(policy, evidence_authorities=[*policy.evidence_authorities, *[
        change(policy.evidence_authorities[0], evidence_id=row.evidence_id,
               record_sha256=digest(row), authoritative=False, reliable=False,
               qualifying_claimreview=False, interested_party=True,
               complete_member_coverage=False, signed_publication_sequence=False,
               reviewed_incident=False) for row in (member, chronology)
    ]])
    request = change(request, representatives=[change(request.representatives[0],
        evidence_id=member.evidence_id, excerpt=draft.claim_text)])
    source_for = {"claim_identity": "member:1", "shared_narrative": "member:1",
                  "publication_sequence": "sequence:1", "limitations": "sequence:1",
                  "verified_context": "sequence:1"}
    draft = change(draft, representation_type="exact_quote", claim_member_evidence_ids=["member:1"],
                   assertions=[change(row, evidence_ids=[source_for.get(row.assertion_class, "src:1")])
                               for row in draft.assertions])
    bindings = [change(row, evidence_id=source_for.get(row.assertion_id, "src:1")) for row in bindings]
    if lane == "claim_misleading":
        primary = change(records["src:1"], source_class="primary")
        independent = change(primary, evidence_id="corroboration:1",
                             source_class="independent_reporting", independence_group="independent:publisher")
        records.update({primary.evidence_id: primary, independent.evidence_id: independent})
        primary_authority = change(policy.evidence_authorities[0], record_sha256=digest(primary),
                                   qualifying_claimreview=False)
        policy = change(policy, evidence_authorities=[primary_authority, *policy.evidence_authorities[1:],
            change(primary_authority, evidence_id=independent.evidence_id,
                   record_sha256=digest(independent), authoritative=False)])
        request = change(request, authority_evidence_ids=["src:1", independent.evidence_id])
        draft = change(draft, assertions=[change(row, evidence_ids=["src:1", independent.evidence_id])
            if row.assertion_class in {"truth_status", "verdict_explanation"} else row for row in draft.assertions])
        bindings += [change(row, evidence_id=independent.evidence_id) for row in bindings
                     if row.assertion_id in {"truth_status", "verdict_explanation"}]
    return request, draft, records, policy, bindings


@pytest.mark.parametrize("lane", ["claim_refuted", "claim_misleading", "propagation_only", "hostile_incident"])
def test_descriptive_sources_do_not_need_verdict_authority(lane):
    request, draft, records, policy, bindings = separate_sources_case(lane)
    validate_approved_assertions(draft, bindings, request, records, policy)


@pytest.mark.parametrize("authority_state", ["absent", "stale"])
def test_exact_quote_cannot_cite_member_without_current_host_authority(authority_state):
    request, draft, records, policy, bindings = setup_case()
    member = change(records["src:1"], evidence_id="member:unbound", exact_passage=draft.claim_text)
    records[member.evidence_id] = member
    request = change(request, representatives=[change(request.representatives[0],
        evidence_id=member.evidence_id, excerpt=draft.claim_text)])
    draft = change(draft, representation_type="exact_quote", claim_member_evidence_ids=[member.evidence_id])
    if authority_state == "stale":
        policy = change(policy, evidence_authorities=[*policy.evidence_authorities,
            change(policy.evidence_authorities[0], evidence_id=member.evidence_id,
                   record_sha256=digest(member), current=False)])
    with pytest.raises(PolicyViolation, match="authority"):
        validate_approved_assertions(draft, bindings, request, records, policy)


@pytest.mark.parametrize("relation,decision", [("context_only", "accepted"), ("supports", "rejected")])
def test_each_member_citation_needs_accepted_claim_identity_support(relation, decision):
    request, draft, records, policy, bindings = setup_case()
    member = change(records["src:1"], evidence_id="member:extra")
    records[member.evidence_id] = member
    policy = change(policy, evidence_authorities=[*policy.evidence_authorities,
        change(policy.evidence_authorities[0], evidence_id=member.evidence_id, record_sha256=digest(member))])
    request = change(request, representatives=[*request.representatives,
        change(request.representatives[0], evidence_id=member.evidence_id)])
    draft = change(draft, claim_member_evidence_ids=["src:1", member.evidence_id], assertions=[
        change(row, evidence_ids=["src:1", member.evidence_id]) if row.assertion_class == "claim_identity" else row
        for row in draft.assertions])
    bindings.append(change(bindings[0], evidence_id=member.evidence_id, relation=relation, reviewer_decision=decision))
    with pytest.raises(PolicyViolation, match="claim.member.*binding"):
        validate_approved_assertions(draft, bindings, request, records, policy)


def test_verified_member_cannot_be_unbound_to_claim_identity():
    request, draft, records, policy, bindings = setup_case()
    member = change(records["src:1"], evidence_id="member:unbound")
    records[member.evidence_id] = member
    policy = change(policy, evidence_authorities=[*policy.evidence_authorities,
        change(policy.evidence_authorities[0], evidence_id=member.evidence_id, record_sha256=digest(member))])
    request = change(request, representatives=[change(request.representatives[0], evidence_id=member.evidence_id)])
    draft = change(draft, claim_member_evidence_ids=[member.evidence_id])
    with pytest.raises(PolicyViolation, match="claim.member.*binding"):
        validate_approved_assertions(draft, bindings, request, records, policy)


@pytest.mark.parametrize("assertion_class", ["truth_status", "verdict_explanation"])
def test_verdict_cannot_use_only_descriptive_source_authority(assertion_class):
    request, draft, records, policy, bindings = separate_sources_case()
    draft = change(draft, assertions=[change(row, evidence_ids=["sequence:1"])
        if row.assertion_class == assertion_class else row for row in draft.assertions])
    bindings = [change(row, evidence_id="sequence:1") if row.assertion_id == assertion_class else row for row in bindings]
    with pytest.raises(PolicyViolation, match="lane authority"):
        validate_approved_assertions(draft, bindings, request, records, policy)
