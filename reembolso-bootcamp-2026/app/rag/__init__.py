"""Recuperação — carga do índice e busca híbrida.

Carrega o que `ingest/build.py` gravou em `storage/` e monta a busca: BM25 e
denso, com fusão e reranker.

Atenção ao persistir: o índice vetorial não é a única coisa que precisa
sobreviver ao build. Se o BM25 for reconstruído a partir de nós em memória,
dentro do container ele não existe — e a busca deixa de ser híbrida sem
levantar erro nenhum.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.rag.hybrid import HybridRetriever

STORAGE_DIR = Path(__file__).resolve().parents[2] / "storage"


@lru_cache(maxsize=1)
def get_retriever() -> HybridRetriever:
    """Carrega o índice sob demanda; /health permanece independente do RAG."""
    return HybridRetriever(STORAGE_DIR)


def reset_retriever() -> None:
    get_retriever.cache_clear()


__all__ = ["HybridRetriever", "get_retriever", "reset_retriever"]
