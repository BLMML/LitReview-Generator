"""Regression for real-model paraphrases leaking into literal PDF modules."""

import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from tests import test_m10_reviews as m10

from apps.common.http import ApiError
from apps.jobs.services import claim_execution
from apps.llm.mock import default_payload
from apps.llm.schemas import ModelTaskRequest, validate_payload
from apps.reviews import services
from apps.reviews.grounding import ground_sections
from apps.reviews.models import Review
from apps.reviews.schemas import SectionsValidationError
from apps.reviews.source_selection import passage_catalog, resolve_selections
from apps.reviews.structure import COMMENTARY_KEYS, SECTION_KEYS, SOURCE_KEYS

run_ready = m10.run_ready


def test_passage_ids_bind_evidence_and_full_text_and_survive_retry():
    eid = str(uuid.uuid4())
    chunks = [{"evidence_id": eid, "quotable_passages": ["It does not improve outcomes."]}]
    catalog, offered = passage_catalog(chunks)
    assert passage_catalog(copy.deepcopy(chunks)) == (catalog, offered)
    pid = offered[0]["quotable_passages"][0]["source_passage_id"]
    assert resolve_selections([{"kind": "claim", "source_passage_id": pid}], "approach", catalog) == [
        {"kind": "claim", "text": "It does not improve outcomes.", "evidence_ids": [eid]}
    ]
    assert passage_catalog([{**chunks[0], "evidence_id": str(uuid.uuid4())}])[0] != catalog
    assert passage_catalog([{**chunks[0], "quotable_passages": ["It improves outcomes."]}])[0] != catalog


@pytest.mark.parametrize(
    "invalid", ["unknown", "duplicate", "altered", "citation", "commentary", "cross_section"]
)
def test_selection_rejects_unoffered_or_ambiguous_source(invalid):
    catalog, _ = passage_catalog([{"evidence_id": str(uuid.uuid4()), "quotable_passages": ["Original."]}])
    block = {"kind": "claim", "source_passage_id": next(iter(catalog))}
    key, blocks = "approach", [block]
    if invalid == "unknown":
        block["source_passage_id"] = "src_" + "0" * 32
    elif invalid == "duplicate":
        blocks.append(copy.deepcopy(block))
    elif invalid == "altered":
        block["text"] = "An invented paraphrase."
    elif invalid == "citation":
        block["evidence_ids"] = [str(uuid.uuid4())]
    elif invalid == "commentary":
        key = "limitations"
    else:
        catalog = {}
    with pytest.raises(SectionsValidationError):
        resolve_selections(blocks, key, catalog)


def test_gateway_accepts_selection_and_rejects_ambiguous_text():
    request = ModelTaskRequest("review_section", "test", "1.0", {}, {"max_blocks": 2})
    block = {"kind": "claim", "source_passage_id": "src_" + "a" * 32}
    payload = {"section_key": "approach", "title": "研究思路", "blocks": [block]}
    validate_payload(request, payload)
    block["text"] = "The model must not substitute its text."
    with pytest.raises(ApiError):
        validate_payload(request, payload)


def test_global_block_budget_reserves_required_commentary():
    config = {"max_blocks_per_section": 20, "max_blocks_total": 100}
    prior = [{"blocks": [{}] * 20} for _ in range(4)]
    assert services._remaining_section_blocks(config, "research_status", prior) == 12
    prior.append({"blocks": [{}] * 12})
    assert services._remaining_section_blocks(config, "achievements", prior) == 1
    prior.append({"blocks": [{}]})
    assert services._remaining_section_blocks(config, "limitations", prior) == 4
    prior.append({"blocks": [{}] * 4})
    assert services._remaining_section_blocks(config, "innovation", prior) == 3


def test_mock_uses_offered_complete_passage_instead_of_retyping():
    eid = str(uuid.uuid4())
    _, offered = passage_catalog(
        [
            {
                "evidence_id": eid,
                "text": "Method\nIt does not improve outcomes.",
                "quotable_passages": ["It does not improve outcomes."],
            }
        ]
    )
    request = ModelTaskRequest(
        "review_section",
        "test",
        "1.0",
        {},
        {"section_key": "approach", "evidence_package": json.dumps(offered)},
        allowed_evidence_ids=[eid],
    )
    result = default_payload(request)
    assert result["blocks"] == [
        {"kind": "claim", "source_passage_id": offered[0]["quotable_passages"][0]["source_passage_id"]}
    ]


@pytest.mark.django_db(transaction=True)
def test_generate_selection_copies_pdf_and_excludes_old_analysis(client_a, run_ready, monkeypatch):
    m10._patch_sections(monkeypatch)
    requests = []

    def choose(context, request):
        requests.append(request)
        key = request.payload["section_key"]
        assert "analysis_summary" not in request.payload
        if key in SOURCE_KEYS:
            package = json.loads(request.payload["evidence_package"])
            choices = [chunk["quotable_passages"][0] for chunk in package[:2]]
            blocks = [{"kind": "claim", "source_passage_id": item["source_passage_id"]} for item in choices]
            payload = {"section_key": key, "title": "Model title is never trusted", "blocks": blocks}
        else:
            payload = m10._section_payload(key, list(request.allowed_evidence_ids))
        return SimpleNamespace(payload=payload)

    monkeypatch.setattr(services, "call_structured", choose)
    accepted = m10._create_review(client_a, run_ready).json()["data"]
    review = Review.objects.get(pk=accepted["resource_id"])
    revision = services.generate_review(claim_execution(accepted["job_id"]), review.pk)
    assert [s["section_key"] for s in revision.sections] == list(SECTION_KEYS)
    assert len(requests) == 8
    assert revision.config_snapshot["source_selection_revision"] == "pdf-passage-selection-v1"
    assert ground_sections(run_ready, revision.sections) == revision.config_snapshot["source_citations"]
    for section, request in zip(revision.sections, requests, strict=True):
        assert all("source_passage_id" not in block for block in section["blocks"])
        if section["section_key"] in SOURCE_KEYS:
            package = json.loads(request.payload["evidence_package"])
            assert [b["text"] for b in section["blocks"]] == [
                c["quotable_passages"][0]["text"] for c in package[:2]
            ]
        if section["section_key"] in COMMENTARY_KEYS:
            prior = json.loads(request.payload["prior_sections"])
            assert all("block_id" not in b for s in prior for b in s["blocks"])


@pytest.mark.django_db(transaction=True)
def test_selection_does_not_weaken_original_text_validation(run_ready):
    from apps.parsing.models import EvidenceChunk

    chunk = EvidenceChunk.objects.filter(
        parsed_document_id__in=run_ready.run_papers.values("parsed_document_id")
    ).first()
    paraphrase = {
        "section_key": "approach",
        "blocks": [
            {
                "kind": "claim",
                "text": "A comparative analysis proves a pioneering new approach.",
                "evidence_ids": [str(chunk.pk)],
            }
        ],
    }
    with pytest.raises(SectionsValidationError, match="原文表述"):
        services._validated_section(paraphrase, "approach", run_ready, max_blocks=20)
