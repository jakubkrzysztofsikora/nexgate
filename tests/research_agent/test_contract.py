from uuid import UUID

import pytest
from pydantic import ValidationError

from research_agent.models import ClusterResearchDraftRequest, MemberCoverage


def request_payload():
    return {
        "request_id": str(UUID(int=1)), "cluster_id": str(UUID(int=2)),
        "cluster_revision": 1, "claim_id": "claim:1",
        "proposition_digest": "a" * 64, "authority_evidence_ids": ["src:1"],
        "evidence_basis": "claim_refuted", "claim_hypothesis": "Display hypothesis",
        "member_coverage": {
            "eligible_member_count": 10, "supporting_count": 8,
            "disputing_count": 0, "mentioning_count": 1, "unclear_count": 1,
            "excluded_count": 2, "support_ratio": 0.8, "minimum_support_ratio": 0.7,
            "mixed_polarity_veto": False, "coverage_rule_version": "cluster-member-stance-v1",
        },
        "representatives": [{
            "evidence_id": "src:1", "source_label": "Public record",
            "url": "https://example.org/record", "published_at": "2026-09-10T10:00:00Z",
            "title": "Record", "excerpt": "The number is 4.", "stance": "supports",
            "selection_reason": "centroid",
        }],
    }


@pytest.mark.parametrize("extra", ["raw_comments", "comment_urls", "author_hashes", "manifest"])
def test_request_rejects_private_or_host_owned_extra_fields(extra):
    payload = request_payload()
    payload[extra] = ["private"]
    with pytest.raises(ValidationError):
        ClusterResearchDraftRequest.model_validate(payload)


def test_nested_comments_and_unbounded_text_are_rejected():
    payload = request_payload()
    payload["representatives"][0]["raw_comment"] = "private"
    with pytest.raises(ValidationError):
        ClusterResearchDraftRequest.model_validate(payload)
    payload = request_payload()
    payload["claim_hypothesis"] = "x" * 100_000
    with pytest.raises(ValidationError):
        ClusterResearchDraftRequest.model_validate(payload)


@pytest.mark.parametrize("change", [
    {"supporting_count": 9}, {"support_ratio": 0.9}, {"support_ratio": float("nan")},
    {"eligible_member_count": True}, {"supporting_count": "8"},
    {"mixed_polarity_veto": "false"}, {"excluded_count": -1},
])
def test_coverage_rejects_coercion_and_inconsistent_denominator(change):
    coverage = request_payload()["member_coverage"] | change
    with pytest.raises(ValidationError):
        MemberCoverage.model_validate(coverage)


def test_request_json_round_trip_is_strict_but_accepts_wire_uuid_and_datetime():
    request = ClusterResearchDraftRequest.model_validate(request_payload())
    assert ClusterResearchDraftRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError):
        request.claim_id = "changed"


def test_lists_digests_and_urls_have_bounds():
    for change in ({"representatives": request_payload()["representatives"] * 1000},
                   {"proposition_digest": "not-a-digest"}, {"authority_evidence_ids": []}):
        with pytest.raises(ValidationError):
            ClusterResearchDraftRequest.model_validate(request_payload() | change)
    payload = request_payload()
    payload["representatives"][0]["url"] = "https://example.org/" + "x" * 10_000
    with pytest.raises(ValidationError):
        ClusterResearchDraftRequest.model_validate(payload)


def test_wire_lists_do_not_coerce_tuples_and_version_does_not_coerce_boolean():
    for change in ({"authority_evidence_ids": ("src:1",)}, {"schema_version": True},
                   {"schema_version": 1.0}):
        with pytest.raises(ValidationError):
            ClusterResearchDraftRequest.model_validate(request_payload() | change)
