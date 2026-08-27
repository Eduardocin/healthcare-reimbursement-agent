from __future__ import annotations

import json
import unittest
from pathlib import Path

from llama_index.core import StorageContext
from llama_index.core.schema import NodeWithScore, TextNode

from app.rag.embedding import DIMENSIONS, LocalHashEmbedding
from app.rag.hybrid import HybridRetriever, RuleAwareReranker, reciprocal_rank_fusion
from app.llm import MODELO_EMBEDDING
from ingest.extract import build_nodes

ROOT = Path(__file__).resolve().parents[1]


class IngestionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.nodes = build_nodes(ROOT / "kb")

    def test_extracts_all_sources_with_stable_unique_ids(self) -> None:
        sources = {str(node.metadata["source"]) for node in self.nodes}
        node_ids = [node.node_id for node in self.nodes]

        self.assertEqual(len(sources), 10)
        self.assertEqual(len(node_ids), len(set(node_ids)))
        self.assertGreater(len(node_ids), 2_000)

    def test_preserves_procedure_as_its_own_searchable_unit(self) -> None:
        matches = [
            node for node in self.nodes
            if node.metadata.get("procedure_code") == "10101012"
        ]

        self.assertEqual(len(matches), 1)
        self.assertIn("Consulta médica em consultório", matches[0].get_content())
        self.assertIn("TUSS-10101012", matches[0].metadata["rule_ids"])

    def test_circular_metadata_does_not_mislabel_internal_articles(self) -> None:
        circular_nodes = [
            node for node in self.nodes
            if node.metadata.get("source") == "circular_02_2026.pdf"
        ]

        self.assertTrue(circular_nodes)
        self.assertTrue(all(
            "CIRC-02-2026" in node.metadata["rule_ids"]
            for node in circular_nodes
        ))
        self.assertTrue(all(
            "ART-1" not in node.metadata["rule_ids"].split(",")
            for node in circular_nodes
        ))
        self.assertEqual(circular_nodes[0].metadata["effective_date"], "2026-04-20")


class HybridRankingTest(unittest.TestCase):
    def test_local_dense_embedding_is_deterministic_and_normalized(self) -> None:
        embedding = LocalHashEmbedding()
        first = embedding.get_text_embedding("sessão de psicoterapia individual")
        second = embedding.get_text_embedding("sessão de psicoterapia individual")

        self.assertEqual(first, second)
        self.assertEqual(len(first), DIMENSIONS)
        self.assertAlmostEqual(sum(value * value for value in first), 1.0, places=6)

    def test_rrf_rewards_nodes_returned_by_both_retrievers(self) -> None:
        shared = TextNode(id_="shared", text="regra compartilhada")
        dense_only = TextNode(id_="dense", text="regra densa")
        sparse_only = TextNode(id_="sparse", text="regra lexical")

        fused = reciprocal_rank_fusion({
            "dense": [
                NodeWithScore(node=dense_only, score=0.9),
                NodeWithScore(node=shared, score=0.8),
            ],
            "bm25": [
                NodeWithScore(node=sparse_only, score=20),
                NodeWithScore(node=shared, score=15),
            ],
        })

        self.assertEqual(fused[0].node.node_id, "shared")
        self.assertEqual(fused[0].ranks, {"dense": 2, "bm25": 2})

    def test_reranker_prioritizes_exact_rule_and_normative_authority(self) -> None:
        authoritative = TextNode(
            id_="norm",
            text="O procedimento TUSS-50000462 observa o teto vigente.",
            metadata={
                "rule_ids": "TUSS-50000462,CIRC-02-2026",
                "authority_rank": 3,
                "effective_date": "2026-04-20",
            },
        )
        support = TextNode(
            id_="faq",
            text="Pergunta frequente sobre terapia e reembolso.",
            metadata={"rule_ids": "", "authority_rank": 1, "effective_date": ""},
        )
        fused = reciprocal_rank_fusion({
            "dense": [
                NodeWithScore(node=support, score=0.9),
                NodeWithScore(node=authoritative, score=0.8),
            ]
        })

        reranked = RuleAwareReranker().rerank(
            fused,
            "Qual é o teto do TUSS-50000462?",
        )

        self.assertEqual(reranked[0].node.node_id, "norm")

    def test_exact_rule_lookup_prefers_primary_normative_source(self) -> None:
        regulation = TextNode(
            id_="regulation",
            text="Art. 78. Regra de alçada.",
            metadata={
                "rule_ids": "ART-78",
                "document_type": "regulamento",
            },
        )
        table_reference = TextNode(
            id_="table",
            text="Material sujeito ao art. 78.",
            metadata={
                "rule_ids": "ART-78,TUSS-70000031",
                "document_type": "tabela_urs",
            },
        )
        retriever = HybridRetriever.__new__(HybridRetriever)
        retriever._all_nodes = (table_reference, regulation)

        matches = retriever._exact_rule_candidates("Aplicar ART-78")

        self.assertEqual(matches[0].node.node_id, "regulation")


class PersistedStorageTest(unittest.TestCase):
    def test_manifest_describes_committed_index(self) -> None:
        manifest_path = ROOT / "storage" / "manifest.json"
        if not manifest_path.exists():
            self.skipTest("storage ainda não foi construído")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["index_id"], "reembolso-kb")
        self.assertEqual(manifest["embedding_model"], MODELO_EMBEDDING)
        self.assertEqual(manifest["embedding_transport"], "openai-compatible-v1")
        self.assertEqual(manifest["source_count"], 10)
        self.assertGreater(manifest["node_count"], 2_000)
        self.assertGreater(manifest["dense_node_count"], 300)
        self.assertLess(manifest["dense_node_count"], manifest["node_count"])
        self.assertTrue((ROOT / "storage" / "bm25").is_dir())
        storage_context = StorageContext.from_defaults(
            persist_dir=str(ROOT / "storage")
        )
        self.assertEqual(len(storage_context.docstore.docs), manifest["node_count"])

    def test_procedure_catalog_contains_table_specific_ceiling(self) -> None:
        catalog = json.loads(
            (ROOT / "storage" / "procedures.json").read_text(encoding="utf-8")
        )
        procedure = catalog["30201238"]
        self.assertEqual(procedure["category"], "EXAME_DIAGNOSTICO")
        self.assertEqual(procedure["ceiling_brl"], "1046.10")
        self.assertIn("TUSS-30201238", procedure["rule_ids"])


if __name__ == "__main__":
    unittest.main()
