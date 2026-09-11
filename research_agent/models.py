"""Version-one wire models; ProductPolicy and EvidenceAuthority are host inputs only."""

from math import isclose
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints,
    UrlConstraints, field_validator, model_validator,
)

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]*$")]
ShortText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500, pattern=r"\S")]
Prose = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8000, pattern=r"\S")]
Passage = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=16000, pattern=r"\S")]
Digest = Annotated[str, StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
URL = Annotated[HttpUrl, UrlConstraints(max_length=2048)]
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
Ratio = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
Flag = Annotated[bool, Field(strict=True)]
EvidenceIDs = Annotated[list[Identifier], Field(strict=True, min_length=1, max_length=128)]
Lane = Literal["claim_refuted", "claim_misleading", "propagation_only", "hostile_incident"]
TruthStatus = Literal["false", "misleading", "unverified", "not_applicable"]
AssertionClass = Literal[
    "claim_identity", "member_coverage", "shared_narrative", "publication_sequence",
    "truth_status", "verdict_explanation", "verified_context", "limitations",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always")


class MemberCoverage(StrictModel):
    eligible_member_count: Count
    supporting_count: Count
    disputing_count: Count
    mentioning_count: Count
    unclear_count: Count
    excluded_count: Count
    support_ratio: Ratio
    minimum_support_ratio: Ratio
    mixed_polarity_veto: Flag
    coverage_rule_version: Identifier

    @model_validator(mode="after")
    def complete_denominator(self) -> Self:
        total = self.supporting_count + self.disputing_count + self.mentioning_count + self.unclear_count
        if total != self.eligible_member_count:
            raise ValueError("coverage counts must equal eligible denominator")
        ratio = self.supporting_count / total if total else 0.0
        if not isclose(self.support_ratio, ratio, rel_tol=0, abs_tol=1e-12):
            raise ValueError("support_ratio must use complete eligible denominator")
        return self


class RepresentativeMember(StrictModel):
    evidence_id: Identifier
    source_label: ShortText
    url: URL
    published_at: AwareDatetime
    title: ShortText
    excerpt: Prose
    stance: Literal["supports", "disputes", "mentions", "unclear"]
    selection_reason: Literal["centroid", "boundary", "contrary_stance", "outlet_diversity", "earliest", "latest"]


class CommentAggregate(StrictModel):
    normalized_template_count: Count
    parent_post_count: Count
    outlet_count: Count
    distinct_author_lower_bound: Count
    timestamp_precision: Literal["exact", "hour", "day", "mixed", "unknown"]
    minimum_gap_lower_bound_seconds: Count | None
    minimum_gap_upper_bound_seconds: Count | None
    definitely_within_1h_count: Count
    possibly_within_1h_count: Count
    unresolved_count: Count
    dedup_method_version: Identifier
    timing_method_version: Identifier

    @model_validator(mode="after")
    def coherent_intervals(self) -> Self:
        lower, upper = self.minimum_gap_lower_bound_seconds, self.minimum_gap_upper_bound_seconds
        if lower is None and upper is not None or lower is not None and upper is not None and lower > upper:
            raise ValueError("invalid comment timing interval")
        if self.definitely_within_1h_count > self.possibly_within_1h_count:
            raise ValueError("definite count exceeds possible count")
        if self.possibly_within_1h_count + self.unresolved_count > self.normalized_template_count:
            raise ValueError("timing count exceeds template count")
        if self.outlet_count > self.parent_post_count:
            raise ValueError("outlet count exceeds parent count")
        return self


class ClusterResearchDraftRequest(StrictModel):
    schema_version: Literal[1] = 1
    task: Literal["cluster_research_draft"] = "cluster_research_draft"
    request_id: UUID
    product_id: Literal["lustro"] = "lustro"
    cluster_id: UUID
    cluster_revision: Annotated[int, Field(strict=True, ge=1, le=1_000_000_000)]
    claim_id: Identifier
    proposition_digest: Digest
    authority_evidence_ids: EvidenceIDs
    evidence_basis: Lane
    claim_hypothesis: Prose
    member_coverage: MemberCoverage
    representatives: Annotated[list[RepresentativeMember], Field(strict=True, min_length=1, max_length=64)]
    comment_aggregates: Annotated[list[CommentAggregate], Field(strict=True, max_length=64)] = Field(default_factory=list)
    language: Literal["pl"] = "pl"

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class AtomicAssertion(StrictModel):
    assertion_id: Identifier
    proposition: Prose
    assertion_class: AssertionClass
    evidence_ids: EvidenceIDs


class ClusterResearchDraft(StrictModel):
    schema_version: Literal[1] = 1
    request_id: UUID
    claim_text: Prose
    representation_type: Literal["exact_quote", "faithful_paraphrase", "analyst_synthesis"]
    claim_member_evidence_ids: EvidenceIDs
    shared_narrative: Prose
    publication_sequence: Prose
    truth_status: TruthStatus
    verdict_explanation: Prose | None = None
    verified_context: Prose | None = None
    limitations: Prose
    assertions: Annotated[list[AtomicAssertion], Field(strict=True, max_length=128)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class ResearchEvidenceRecord(StrictModel):
    evidence_id: Identifier
    url: URL
    final_url: URL
    redirect_chain: Annotated[list[URL], Field(strict=True, max_length=8)]
    title: ShortText
    publisher: ShortText
    author: ShortText | None
    published_at: AwareDatetime | None
    updated_at: AwareDatetime | None
    retrieved_at: AwareDatetime
    source_class: Literal["primary", "claimreview", "official", "independent_reporting", "commentary"]
    independence_group: Identifier
    exact_passage: Passage
    passage_locator: ShortText
    content_sha256: Digest
    extracted_text_sha256: Digest
    extractor_version: Identifier
    archive_ref: Annotated[str, StringConstraints(strict=True, min_length=90, max_length=90, pattern=r"^editorial-research/sha256/[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def content_addressed_archive(self) -> Self:
        if self.archive_ref != "editorial-research/sha256/" + self.content_sha256:
            raise ValueError("archive_ref must match captured content digest")
        return self


class AssertionEvidenceBinding(StrictModel):
    assertion_id: Identifier
    evidence_id: Identifier
    relation: Literal["supports", "contradicts", "context_only"]
    reviewer_decision: Literal["accepted", "rejected"]


class EvidenceAuthority(StrictModel):
    """Host attestations made after archive verification and source/claim review.

    Never accept these fields from synthesis. record_sha256 binds every field of
    the verified evidence record, including the passage and independence group.
    """

    evidence_id: Identifier
    claim_id: Identifier
    proposition_digest: Digest
    record_sha256: Digest
    capture_verified: Flag = False
    current: Flag = False
    authoritative: Flag = False
    reliable: Flag = False
    qualifying_claimreview: Flag = False
    interested_party: Flag = False
    unresolved_conflict: Flag = False
    complete_member_coverage: Flag = False
    signed_publication_sequence: Flag = False
    reviewed_incident: Flag = False


class ProductPolicy(StrictModel):
    """Trusted per-request policy, assembled by the host; never a wire artifact."""

    product_id: Literal["lustro"] = "lustro"
    allowed_tasks: Annotated[list[Literal["cluster_research_draft"]], Field(strict=True, min_length=1, max_length=1)] = Field(default_factory=lambda: ["cluster_research_draft"])
    reviewed_claim_id: Identifier
    reviewed_claim_text: Prose
    evidence_authorities: Annotated[list[EvidenceAuthority], Field(strict=True, max_length=256)] = Field(default_factory=list)
    max_request_bytes: Annotated[int, Field(strict=True, ge=1, le=2_000_000)] = 131_072
    max_result_bytes: Annotated[int, Field(strict=True, ge=1, le=2_000_000)] = 131_072
    minimum_parent_posts: Annotated[int, Field(strict=True, ge=3, le=1_000_000_000)] = 3
    minimum_outlets: Annotated[int, Field(strict=True, ge=3, le=1_000_000_000)] = 3
    minimum_authors: Annotated[int, Field(strict=True, ge=5, le=1_000_000_000)] = 5
    minimum_support_ratio: Ratio = 0.0
