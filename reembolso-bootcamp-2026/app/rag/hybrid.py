"""Busca híbrida local: densa + BM25, fusão RRF e reranking."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

from llama_index.core import StorageContext, load_index_from_storage
from llama_index.core.schema import BaseNode, NodeWithScore
from llama_index.retrievers.bm25 import BM25Retriever

from app.rag.embedding import GatewayEmbedding, LocalHashEmbedding
from app.llm import MODELO_EMBEDDING
from app.rag.models import HybridSearchResult, RetrievedChunk

INDEX_ID = "reembolso-kb"
EXPECTED_SCHEMA_VERSION = 1
TOKEN_PATTERN = re.compile(r"\b\w{2,}\b", re.UNICODE)
RULE_PATTERN = re.compile(
    r"\b(?:ART-\d+|CIRC-\d{2}-\d{4}|TUSS-\d{8}|NT-\d+|ANEXO-[IVX]+)\b"
)


@dataclass
class FusedNode:
    node: BaseNode
    rrf_score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)
    source_scores: dict[str, float] = field(default_factory=dict)
    rerank_score: float = 0.0


def _normalized_tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return set(TOKEN_PATTERN.findall(without_accents))


def reciprocal_rank_fusion(
    result_sets: Mapping[str, Sequence[NodeWithScore]],
    *,
    weights: Mapping[str, float] | None = None,
    rank_constant: int = 60,
) -> list[FusedNode]:
    """Combina rankings sem comparar escalas incompatíveis de score."""
    configured_weights = weights or {}
    fused: dict[str, FusedNode] = {}
    for source_name, results in result_sets.items():
        weight = configured_weights.get(source_name, 1.0)
        for rank, item in enumerate(results, start=1):
            candidate = fused.setdefault(item.node.node_id, FusedNode(node=item.node))
            candidate.rrf_score += weight / (rank_constant + rank)
            candidate.ranks[source_name] = rank
            candidate.source_scores[source_name] = float(item.score or 0.0)
    return sorted(fused.values(), key=lambda item: (-item.rrf_score, item.node.node_id))


class RuleAwareReranker:
    """Segundo estágio determinístico com relevância, autoridade e especificidade."""

    def rerank(
        self,
        candidates: Sequence[FusedNode],
        query: str,
        service_date: date | None = None,
    ) -> list[FusedNode]:
        if not candidates:
            return []
        query_tokens = _normalized_tokens(query)
        query_rules = set(RULE_PATTERN.findall(query.upper()))
        max_rrf = max(candidate.rrf_score for candidate in candidates) or 1.0
        latest_circular = self._latest_applicable_circular(
            candidates,
            service_date,
        ) if query_tokens & {"atual", "data", "vigencia", "vigente"} else None

        for candidate in candidates:
            text = candidate.node.get_content()
            text_tokens = _normalized_tokens(text)
            coverage = len(query_tokens & text_tokens) / max(len(query_tokens), 1)
            metadata = candidate.node.metadata
            node_rules = {
                value for value in str(metadata.get("rule_ids", "")).split(",") if value
            }
            exact_rule_matches = len(query_rules & node_rules)
            authority_rank = int(metadata.get("authority_rank", 1))
            effective_bonus = self._effective_bonus(metadata, service_date)
            if (
                latest_circular
                and metadata.get("document_type") == "circular"
                and metadata.get("effective_date") == latest_circular
            ):
                effective_bonus += 0.30

            candidate.rerank_score = (
                0.60 * (candidate.rrf_score / max_rrf)
                + 0.25 * coverage
                + 0.30 * exact_rule_matches
                + 0.025 * authority_rank
                + effective_bonus
            )

        return sorted(
            candidates,
            key=lambda item: (-item.rerank_score, -item.rrf_score, item.node.node_id),
        )

    @staticmethod
    def _effective_bonus(metadata: Mapping[str, object], service_date: date | None) -> float:
        raw_date = str(metadata.get("effective_date", ""))
        if not raw_date:
            return 0.0
        try:
            effective_date = date.fromisoformat(raw_date)
        except ValueError:
            return 0.0
        reference = service_date or date.today()
        return 0.025 if effective_date <= reference else -0.10

    @staticmethod
    def _latest_applicable_circular(
        candidates: Sequence[FusedNode],
        service_date: date | None,
    ) -> str | None:
        reference = service_date or date.today()
        applicable: list[date] = []
        for candidate in candidates:
            metadata = candidate.node.metadata
            if metadata.get("document_type") != "circular":
                continue
            try:
                effective_date = date.fromisoformat(str(metadata.get("effective_date", "")))
            except ValueError:
                continue
            if effective_date <= reference:
                applicable.append(effective_date)
        return max(applicable).isoformat() if applicable else None


class HybridRetriever:
    def __init__(
        self,
        storage_dir: Path,
        *,
        dense_top_k: int = 30,
        sparse_top_k: int = 20,
    ) -> None:
        self.storage_dir = storage_dir
        manifest = self._validate_manifest()
        storage_context = StorageContext.from_defaults(persist_dir=str(storage_dir))
        self._all_nodes = tuple(storage_context.docstore.docs.values())
        embedding_model = (
            GatewayEmbedding()
            if manifest.get("embedding_model") == MODELO_EMBEDDING
            else LocalHashEmbedding()
        )
        index = load_index_from_storage(
            storage_context,
            index_id=INDEX_ID,
            embed_model=embedding_model,
        )
        self._dense = index.as_retriever(similarity_top_k=dense_top_k)
        self._sparse = BM25Retriever.from_persist_dir(
            str(storage_dir / "bm25"),
        )
        self._sparse.similarity_top_k = sparse_top_k
        self._reranker = RuleAwareReranker()

    def _validate_manifest(self) -> dict[str, object]:
        manifest_path = self.storage_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"índice ausente em {self.storage_dir}; execute python -m ingest.build"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != EXPECTED_SCHEMA_VERSION:
            raise ValueError("versão incompatível do índice persistido")
        if manifest.get("index_id") != INDEX_ID:
            raise ValueError("identificador incompatível do índice persistido")
        if manifest.get("embedding_dimensions") != 1536:
            raise ValueError("dimensão incompatível do índice persistido")
        return manifest

    def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        max_input_tokens: int = 8_000,
        service_date: date | None = None,
    ) -> HybridSearchResult:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("a consulta normativa não pode ser vazia")
        if top_k < 1 or max_input_tokens < 1:
            raise ValueError("top_k e max_input_tokens devem ser positivos")

        dense = self._dense.retrieve(normalized_query)
        sparse = self._sparse.retrieve(normalized_query)
        exact = self._exact_rule_candidates(normalized_query)
        fused = reciprocal_rank_fusion(
            {"dense": dense, "bm25": sparse, "exact": exact},
            weights={"dense": 1.0, "bm25": 1.0, "exact": 1.25},
        )
        reranked = self._reranker.rerank(fused, normalized_query, service_date)

        chunks: list[RetrievedChunk] = []
        used_tokens = 0
        source_counts: dict[str, int] = {}
        for candidate in reranked:
            if len(chunks) >= top_k:
                break
            text = candidate.node.get_content().strip()
            estimated_tokens = max(1, math.ceil(len(text) / 4))
            if used_tokens + estimated_tokens > max_input_tokens:
                continue
            metadata = candidate.node.metadata
            source = str(metadata.get("source", "desconhecida"))
            if source_counts.get(source, 0) >= 3:
                continue
            chunks.append(RetrievedChunk(
                node_id=candidate.node.node_id,
                text=text,
                source=source,
                page=int(metadata["page"]) if metadata.get("page") else None,
                rule_ids=[
                    value for value in str(metadata.get("rule_ids", "")).split(",") if value
                ],
                score=round(candidate.rerank_score, 8),
                estimated_tokens=estimated_tokens,
            ))
            used_tokens += estimated_tokens
            source_counts[source] = source_counts.get(source, 0) + 1

        return HybridSearchResult(
            query=normalized_query,
            chunks=chunks,
            estimated_tokens=used_tokens,
        )

    def _exact_rule_candidates(self, query: str) -> list[NodeWithScore]:
        query_rules = set(RULE_PATTERN.findall(query.upper()))
        if not query_rules:
            return []
        candidates: list[NodeWithScore] = []
        for node in self._all_nodes:
            node_rules = {
                value for value in str(node.metadata.get("rule_ids", "")).split(",") if value
            }
            matches = query_rules & node_rules
            if not matches:
                continue
            primary_matches = sum(
                self._is_primary_source(rule_id, node.metadata)
                for rule_id in matches
            )
            score = float(len(matches) + primary_matches)
            candidates.append(NodeWithScore(node=node, score=score))
        return sorted(
            candidates,
            key=lambda item: (-float(item.score or 0.0), item.node.node_id),
        )[:20]

    @staticmethod
    def _is_primary_source(rule_id: str, metadata: Mapping[str, object]) -> bool:
        document_type = metadata.get("document_type")
        if rule_id.startswith("ART-"):
            return document_type == "regulamento"
        if rule_id.startswith("CIRC-"):
            return metadata.get("source_rule_id") == rule_id
        if rule_id.startswith("TUSS-"):
            return metadata.get("procedure_code") == rule_id.removeprefix("TUSS-")
        if rule_id.startswith("NT-"):
            return document_type == "nota_tecnica"
        if rule_id.startswith("ANEXO-"):
            return document_type == "anexo"
        return False
