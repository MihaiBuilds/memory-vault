"""
spaCy-based entity and relationship extraction.

Public surface:
    extract_entities(text) -> list[Entity]
    extract_relationships(entities, text) -> list[Relationship]

Both are synchronous. Callers in async code should wrap with
asyncio.to_thread to avoid blocking the event loop.

If spaCy or the en_core_web_sm model is unavailable, both functions
log a warning once at import and then return empty lists.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from itertools import combinations

logger = logging.getLogger(__name__)

SPACY_MODEL = "en_core_web_sm"

# spaCy NER label → our entity type
_NER_LABEL_MAP = {
    "PERSON": "Person",
    "ORG": "Project",
    "PRODUCT": "Tool",
}

# Concept extraction thresholds
_MIN_CONCEPT_TOKENS = 2
_MIN_CONCEPT_OCCURRENCES = 2


@dataclass
class Entity:
    name: str
    type: str            # "Person" | "Project" | "Tool" | "Concept"
    start: int
    end: int


@dataclass
class Relationship:
    source_name: str
    target_name: str
    type: str            # "related_to" (M7); other types reserved for future milestones


# Lazy-loaded module-level spaCy pipeline. None until first call or failed load.
_nlp = None
_SPACY_READY = False

try:
    import spacy

    try:
        _nlp = spacy.load(SPACY_MODEL)
        _SPACY_READY = True
    except OSError:
        logger.warning(
            "spaCy model %r not installed; extraction disabled. "
            "Install with: python -m spacy download %s",
            SPACY_MODEL,
            SPACY_MODEL,
        )
except ImportError:
    logger.warning("spaCy not installed; entity extraction disabled")


def extract_entities(text: str) -> list[Entity]:
    """Extract entities from a chunk of text.

    Returns a deduplicated list (by case-insensitive name + type) in
    first-occurrence order. Types: Person, Project, Tool, Concept.
    """
    if not _SPACY_READY or not text:
        return []

    doc = _nlp(text)

    entities: list[Entity] = []
    seen: set[tuple[str, str]] = set()

    # NER entities (PERSON, ORG, PRODUCT).
    # Track the character spans we've consumed so Concept extraction
    # can skip noun chunks that overlap NER hits.
    ner_spans: list[tuple[int, int]] = []
    for ent in doc.ents:
        mapped_type = _NER_LABEL_MAP.get(ent.label_)
        if mapped_type is None:
            continue
        name = ent.text.strip()
        if not name:
            continue
        key = (name.lower(), mapped_type)
        ner_spans.append((ent.start_char, ent.end_char))
        if key in seen:
            continue
        seen.add(key)
        entities.append(Entity(name=name, type=mapped_type, start=ent.start_char, end=ent.end_char))

    # Concept extraction: noun chunks ≥ _MIN_CONCEPT_TOKENS that appear
    # ≥ _MIN_CONCEPT_OCCURRENCES times, lowercased for counting, first-seen
    # casing preserved for display, skipping any that overlap NER spans.
    concept_chunks: list[tuple[str, int, int]] = []  # (text, start, end)
    for np in doc.noun_chunks:
        if len(np) < _MIN_CONCEPT_TOKENS:
            continue
        if _overlaps_any(np.start_char, np.end_char, ner_spans):
            continue
        name = np.text.strip()
        if name:
            concept_chunks.append((name, np.start_char, np.end_char))

    counts = Counter(name.lower() for name, _, _ in concept_chunks)
    first_seen: dict[str, tuple[str, int, int]] = {}
    for name, start, end in concept_chunks:
        key = name.lower()
        if key not in first_seen:
            first_seen[key] = (name, start, end)

    for key, count in counts.items():
        if count < _MIN_CONCEPT_OCCURRENCES:
            continue
        concept_key = (key, "Concept")
        if concept_key in seen:
            continue
        seen.add(concept_key)
        name, start, end = first_seen[key]
        entities.append(Entity(name=name, type="Concept", start=start, end=end))

    return entities


def extract_relationships(entities: list[Entity], text: str) -> list[Relationship]:
    """Extract entity-to-entity relationships within a chunk.

    M7 auto-extracts only `related_to` — co-occurrence of any two
    distinct entities within the same chunk. Order is lexicographic on
    (source_name, target_name) for determinism; undirected semantics.

    `text` is accepted but unused in M7 — reserved for future
    directional or proximity-based extraction.
    """
    if not _SPACY_READY or len(entities) < 2:
        return []

    # Use a set of frozensets for dedup against the already-lexicographically-ordered pairs.
    relationships: list[Relationship] = []
    seen_pairs: set[frozenset[str]] = set()

    for a, b in combinations(entities, 2):
        if a.name == b.name:
            continue
        pair = frozenset((a.name, b.name))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        source, target = sorted((a.name, b.name))
        relationships.append(
            Relationship(source_name=source, target_name=target, type="related_to")
        )

    return relationships


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """True if [start, end) overlaps any (s, e) in spans."""
    for s, e in spans:
        if start < e and s < end:
            return True
    return False
