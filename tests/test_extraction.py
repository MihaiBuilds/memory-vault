"""
Unit tests for the spaCy extractor.

These tests run against the real `en_core_web_sm` model (installed in
Dockerfile.test), so they verify actual NER output rather than mocked
behavior. That's intentional — the extractor's value IS what spaCy
produces, so mocking would test the mock, not the code.
"""

from __future__ import annotations

from src.extraction.spacy_extractor import (
    Entity,
    Relationship,
    _SPACY_READY,
    extract_entities,
    extract_relationships,
)


# ---------------------------------------------------------------------------
# extract_entities
# ---------------------------------------------------------------------------


def test_empty_text_returns_empty_list():
    assert extract_entities("") == []


def test_text_with_no_entities_returns_empty_list():
    # "it was fine" has no NER hits and no repeated multi-token noun phrases.
    assert extract_entities("it was fine") == []


def test_person_ner_is_captured():
    assert _SPACY_READY, "spaCy + en_core_web_sm required for this test"
    entities = extract_entities("Barack Obama visited Berlin.")
    names_by_type = {(e.name.lower(), e.type) for e in entities}
    assert ("barack obama", "Person") in names_by_type


def test_ner_label_mapping_unknown_labels_dropped():
    # Berlin is a GPE in spaCy, which our mapping drops.
    entities = extract_entities("Barack Obama visited Berlin.")
    types = {e.type for e in entities}
    # GPE/LOC should never appear as entity types.
    assert types.issubset({"Person", "Project", "Tool", "Concept"})


def test_deduplication_within_chunk_keeps_one_entity():
    text = "Claude helped me. Claude is great. I talked to Claude again."
    entities = extract_entities(text)
    claude_entities = [e for e in entities if e.name.lower() == "claude"]
    # Case-insensitive dedup: only one Claude, regardless of type variant.
    assert len(claude_entities) == 1


def test_concept_requires_min_occurrences():
    # "hybrid search" appears only once — should NOT become a Concept.
    text = "Let's talk about hybrid search today."
    entities = extract_entities(text)
    concept_names = {e.name.lower() for e in entities if e.type == "Concept"}
    assert "hybrid search" not in concept_names


def test_concept_captures_repeated_multi_token_phrase():
    # "hybrid search" appears twice — should become a Concept.
    text = (
        "Hybrid search combines vector and keyword retrieval. "
        "With hybrid search you get the best of both worlds."
    )
    entities = extract_entities(text)
    concept_names = {e.name.lower() for e in entities if e.type == "Concept"}
    assert "hybrid search" in concept_names


def test_concept_first_seen_casing_preserved():
    # Phrasing is chosen so spaCy produces "hybrid search" as the same
    # noun chunk twice (not "hybrid search handles" or "the hybrid search
    # returns") and doesn't NER-tag it as a PERSON (which capital-letter
    # phrasings can trigger).
    text = (
        "The memory system uses hybrid search. "
        "Over time, hybrid search beats pure vector search."
    )
    entities = extract_entities(text)
    concepts = [e for e in entities if e.type == "Concept" and e.name.lower() == "hybrid search"]
    assert len(concepts) == 1
    # First-seen lowercase casing is preserved (source text is already lowercase).
    assert concepts[0].name == "hybrid search"


def test_concept_excludes_spans_overlapping_ner():
    # "Barack Obama" appears twice — NER will tag it as PERSON both times.
    # The noun chunk "Barack Obama" would otherwise also qualify as a
    # Concept (2 tokens, ≥2 occurrences), but the NER-overlap filter
    # must exclude it.
    text = "Barack Obama gave a speech. Later, Barack Obama met the press."
    entities = extract_entities(text)
    concept_names = {e.name.lower() for e in entities if e.type == "Concept"}
    # "barack obama" is a Person via NER — must NOT also appear as a Concept.
    assert "barack obama" not in concept_names


# ---------------------------------------------------------------------------
# extract_relationships
# ---------------------------------------------------------------------------


def test_relationships_empty_when_fewer_than_two_entities():
    assert extract_relationships([], "") == []
    single = [Entity(name="Alice", type="Person", start=0, end=5)]
    assert extract_relationships(single, "Alice") == []


def test_relationships_pairs_all_entities():
    # 3 entities → C(3,2) = 3 pairs, all `related_to`, lexicographically
    # ordered (source < target) for determinism.
    entities = [
        Entity(name="Charlie", type="Person", start=0, end=7),
        Entity(name="Alice", type="Person", start=10, end=15),
        Entity(name="Bob", type="Person", start=20, end=23),
    ]
    rels = extract_relationships(entities, "Charlie met Alice with Bob")
    assert len(rels) == 3
    assert all(r.type == "related_to" for r in rels)
    # Each relationship source_name should be lexicographically <= target_name.
    for r in rels:
        assert r.source_name <= r.target_name
    pairs = {(r.source_name, r.target_name) for r in rels}
    assert pairs == {("Alice", "Bob"), ("Alice", "Charlie"), ("Bob", "Charlie")}


def test_relationships_dedupes_duplicate_entity_pairs():
    # Extractor wouldn't normally produce duplicate entities (it dedupes
    # earlier), but if given them, relationships must not duplicate pairs.
    entities = [
        Entity(name="Alice", type="Person", start=0, end=5),
        Entity(name="Bob", type="Person", start=6, end=9),
        Entity(name="Alice", type="Person", start=12, end=17),  # duplicate
    ]
    rels = extract_relationships(entities, "Alice Bob Alice")
    # Only one unique pair despite the duplicated Alice.
    assert len(rels) == 1
    assert {(r.source_name, r.target_name) for r in rels} == {("Alice", "Bob")}


def test_relationship_dataclass_shape():
    # Dataclass contract — fields must match what graph_writer expects.
    r = Relationship(source_name="A", target_name="B", type="related_to")
    assert r.source_name == "A"
    assert r.target_name == "B"
    assert r.type == "related_to"
