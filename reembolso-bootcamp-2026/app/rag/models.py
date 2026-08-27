"""Saídas estruturadas da recuperação normativa."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    node_id: str
    text: str
    source: str
    page: int | None = None
    rule_ids: list[str] = Field(default_factory=list)
    score: float
    estimated_tokens: int = Field(ge=1)


class HybridSearchResult(BaseModel):
    query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=0)

    def as_context(self) -> str:
        sections: list[str] = []
        for chunk in self.chunks:
            location = chunk.source
            if chunk.page is not None:
                location += f", página {chunk.page}"
            rules = ", ".join(chunk.rule_ids) or "sem identificador explícito"
            sections.append(f"[Fonte: {location}; dispositivos: {rules}]\n{chunk.text}")
        return "\n\n".join(sections)
