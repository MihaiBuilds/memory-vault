"""Pydantic request/response models for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = Field(..., examples=["ok"])
    database: str = Field(..., examples=["connected"])
    embedding_model: str = Field(..., examples=["all-MiniLM-L6-v2"])
    version: str = Field(..., examples=["0.4.0"])


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, examples=["how does hybrid search work"])
    spaces: list[str] | None = Field(default=None, examples=[["default"]])
    since: str | None = Field(default=None, examples=["2026-01-01"])
    limit: int = Field(default=10, ge=1, le=50)


class SearchHit(BaseModel):
    chunk_id: str
    content: str
    similarity: float
    space: str
    speaker: str | None = None
    source: str | None = None
    created_at: datetime | None = None
    metadata: dict[str, Any] = {}


class SearchResponse(BaseModel):
    results: list[SearchHit]
    total_results: int
    query_variations: list[str]
    query_time_ms: int


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


class ChunkSummary(BaseModel):
    chunk_id: str
    content: str
    space: str
    source: str | None = None
    speaker: str | None = None
    importance: float
    created_at: datetime | None = None
    metadata: dict[str, Any] = {}


class ChunkList(BaseModel):
    chunks: list[ChunkSummary]
    total: int
    limit: int
    offset: int


class ForgetResponse(BaseModel):
    success: bool
    chunk_id: str
    message: str


# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------


class SpaceInfo(BaseModel):
    name: str
    description: str | None = None
    chunk_count: int


class SpaceList(BaseModel):
    spaces: list[SpaceInfo]


class SpaceCreateRequest(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        examples=["work", "side-projects"],
        description="Lowercase letters, digits, and hyphens only. Must start with letter or digit.",
    )
    description: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class IngestTextRequest(BaseModel):
    text: str = Field(..., min_length=1)
    space: str = Field(default="default")
    source: str = Field(default="api")
    speaker: str | None = None


class IngestResponse(BaseModel):
    stored: bool
    chunk_id: str | None = None
    chunks_created: int = 0
    message: str


# ---------------------------------------------------------------------------
# Knowledge graph
# ---------------------------------------------------------------------------


class EntitySummary(BaseModel):
    id: str
    name: str
    type: str
    space: str
    mention_count: int
    created_at: datetime | None = None


class EntityList(BaseModel):
    entities: list[EntitySummary]
    total: int
    limit: int
    offset: int


class EntityMention(BaseModel):
    chunk_id: str
    start_offset: int
    end_offset: int
    chunk_preview: str


class RelatedEntity(BaseModel):
    id: str
    name: str
    type: str
    co_mention_count: int


class EntityDetail(BaseModel):
    id: str
    name: str
    type: str
    space: str
    mention_count: int
    created_at: datetime | None = None
    mentions: list[EntityMention]
    related: list[RelatedEntity]


class RelationshipRow(BaseModel):
    id: str
    source_entity_id: str
    target_entity_id: str
    source_name: str
    target_name: str
    type: str
    chunk_id: str | None = None
    created_at: datetime | None = None


class RelationshipList(BaseModel):
    relationships: list[RelationshipRow]
    total: int
    limit: int
    offset: int


class GraphNode(BaseModel):
    id: str
    name: str
    type: str
    mention_count: int


class GraphEdge(BaseModel):
    source: str
    target: str
    type: str
    weight: int


class GraphVisualization(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    node_count: int
    edge_count: int
    truncated: bool
