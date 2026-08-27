"""Constrói o índice a partir de kb/ e grava em storage/.

    python -m ingest.build

Roda FORA do container, na sua máquina. Depois você commita `storage/` e o
Dockerfile só copia — o build da imagem precisa ser offline e determinístico, e
o `/health` tem 60 segundos para responder.

Você vai rodar isto de novo quando a base de conhecimento mudar. Deixe rápido.
"""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

from llama_index.core import StorageContext, VectorStoreIndex
from llama_index.core.schema import BaseNode, MetadataMode
from llama_index.retrievers.bm25 import BM25Retriever

from app.llm import DIMENSOES, MODELO_EMBEDDING
from app.rag.embedding import GatewayEmbedding
from ingest.catalog import build_catalog
from ingest.extract import build_nodes, source_hashes

RAIZ = Path(__file__).resolve().parents[1]
DIR_KB = RAIZ / "kb"
DIR_STORAGE = RAIZ / "storage"
CACHE_PATH = RAIZ / ".embedding-cache.jsonl"
INDEX_ID = "reembolso-kb"
SCHEMA_VERSION = 1
EMBED_BATCH_SIZE = 50
EMBED_WORKERS = 2
EMBEDDING_TRANSPORT = "openai-compatible-v1"


def _prepare_build_directory() -> Path:
    target = RAIZ / ".storage-building"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    return target


def _publish(build_dir: Path) -> None:
    if DIR_STORAGE.exists():
        shutil.rmtree(DIR_STORAGE)
    build_dir.replace(DIR_STORAGE)


def _cache_header(hashes: dict[str, str]) -> dict[str, object]:
    return {
        "type": "header",
        "embedding_model": MODELO_EMBEDDING,
        "embedding_dimensions": DIMENSOES,
        "embedding_transport": EMBEDDING_TRANSPORT,
        "source_sha256": hashes,
    }


def _load_embedding_cache(hashes: dict[str, str]) -> dict[str, list[float]]:
    if not CACHE_PATH.is_file():
        CACHE_PATH.write_text(
            json.dumps(_cache_header(hashes), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {}

    lines = CACHE_PATH.read_text(encoding="utf-8").splitlines()
    try:
        header = json.loads(lines[0]) if lines else None
    except json.JSONDecodeError:
        header = None
    if header != _cache_header(hashes):
        CACHE_PATH.write_text(
            json.dumps(_cache_header(hashes), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {}

    cached: dict[str, list[float]] = {}
    for line in lines[1:]:
        try:
            item = json.loads(line)
            embedding = item["embedding"]
            if isinstance(embedding, list) and len(embedding) == DIMENSOES:
                cached[str(item["node_id"])] = embedding
        except (json.JSONDecodeError, KeyError, TypeError):
            # Uma última linha incompleta pode existir se o processo for encerrado
            # exatamente durante a gravação; os lotes anteriores continuam válidos.
            continue
    return cached


def _append_embedding_cache(batch: Sequence[BaseNode]) -> None:
    with CACHE_PATH.open("a", encoding="utf-8") as cache:
        for node in batch:
            cache.write(json.dumps({
                "node_id": node.node_id,
                "embedding": node.embedding,
            }) + "\n")
        cache.flush()


def _embed_nodes(nodes: Sequence[BaseNode], hashes: dict[str, str]) -> None:
    cached = _load_embedding_cache(hashes)
    for node in nodes:
        if node.node_id in cached:
            node.embedding = cached[node.node_id]

    pending = [node for node in nodes if node.embedding is None]
    if not pending:
        print(f"embeddings recuperados do cache: {len(nodes)}/{len(nodes)}")
        return

    embed_model = GatewayEmbedding(embed_batch_size=EMBED_BATCH_SIZE)
    completed = len(nodes) - len(pending)
    window_size = EMBED_BATCH_SIZE * EMBED_WORKERS
    for start in range(0, len(pending), window_size):
        window = pending[start:start + window_size]
        batches = [
            window[index:index + EMBED_BATCH_SIZE]
            for index in range(0, len(window), EMBED_BATCH_SIZE)
        ]
        failures: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as executor:
            futures = {
                executor.submit(
                    embed_model.get_text_embedding_batch,
                    [node.get_content(metadata_mode=MetadataMode.EMBED) for node in batch],
                ): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    embeddings = future.result()
                    if len(embeddings) != len(batch) or any(
                        len(embedding) != DIMENSOES for embedding in embeddings
                    ):
                        raise ValueError(
                            "o serviço retornou embeddings com dimensão incompatível"
                        )
                    for node, embedding in zip(batch, embeddings):
                        node.embedding = embedding
                    _append_embedding_cache(batch)
                    completed += len(batch)
                    print(
                        f"embeddings concluídos: {completed}/{len(nodes)}",
                        flush=True,
                    )
                except BaseException as exc:
                    failures.append(exc)
        if failures:
            raise failures[0]


def main() -> int:
    nodes = build_nodes(DIR_KB)
    dense_nodes = [
        node for node in nodes
        if not node.metadata.get("procedure_code")
    ]
    hashes = source_hashes(DIR_KB)
    build_dir = _prepare_build_directory()

    try:
        # Procedimentos individuais permanecem no BM25, no docstore e na busca
        # exata por TUSS. A camada densa cobre os trechos normativos, nos quais
        # similaridade semântica agrega valor sem duplicar o catálogo inteiro.
        _embed_nodes(dense_nodes, hashes)
        storage_context = StorageContext.from_defaults()
        index = VectorStoreIndex(
            dense_nodes,
            storage_context=storage_context,
            embed_model=GatewayEmbedding(),
            store_nodes_override=True,
        )
        index.set_index_id(INDEX_ID)
        storage_context.docstore.add_documents(
            [node.model_copy(update={"embedding": None}) for node in nodes],
            allow_update=True,
        )
        storage_context.persist(persist_dir=build_dir)

        bm25 = BM25Retriever.from_defaults(
            nodes=nodes,
            language="portuguese",
            similarity_top_k=20,
        )
        bm25.persist(str(build_dir / "bm25"))

        (build_dir / "procedures.json").write_text(
            json.dumps(build_catalog(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "index_id": INDEX_ID,
            "embedding_model": MODELO_EMBEDDING,
            "embedding_dimensions": DIMENSOES,
            "embedding_transport": EMBEDDING_TRANSPORT,
            "node_count": len(nodes),
            "dense_node_count": len(dense_nodes),
            "source_count": len(hashes),
            "source_sha256": hashes,
        }
        (build_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _publish(build_dir)
        CACHE_PATH.unlink(missing_ok=True)
    except BaseException:
        if build_dir.exists():
            shutil.rmtree(build_dir)
        raise

    print(f"índice construído: {len(nodes)} nós em {DIR_STORAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
