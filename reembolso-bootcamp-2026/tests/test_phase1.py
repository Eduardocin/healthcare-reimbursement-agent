from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agents.models import AgentName
from app.agents.supervisor.service import AgentRuntime
from app.main import app
from app.schemas import Anexo, ChatRequest
from app.tools.mcp_client import MCPClientError


class _FakeLLM:
    def invoke(self, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(
            content=f"Resposta contextualizada para este turno ({abs(hash(prompt))})."
        )


class _UnavailableMCP:
    def consultar_beneficiario_sync(self, carteirinha: str):
        raise MCPClientError("MCP indisponível no teste unitário")


class _EmptyRetriever:
    def search(self, *args, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(chunks=[])


class AgentRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.patchers = [
            patch("app.agents.supervisor.graph.criar_llm", return_value=_FakeLLM()),
            patch("app.agents.normas.agent.criar_llm", return_value=_FakeLLM()),
            patch("app.agents.documento.extractor.criar_llm", return_value=_FakeLLM()),
            patch("app.agents.triagem.agent.MCPOperadoraClient", return_value=_UnavailableMCP()),
            patch("app.agents.normas.agent.get_retriever", return_value=_EmptyRetriever()),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.runtime = AgentRuntime()

    def tearDown(self) -> None:
        self.runtime.reset()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def test_persists_turns_and_isolates_sessions(self) -> None:
        self.runtime.chat(ChatRequest(
            session_id="session-a",
            mensagem="Minha carteirinha é 1111 2222 3333 4444",
        ))
        self.runtime.chat(ChatRequest(
            session_id="session-a",
            mensagem="Quero continuar o meu atendimento",
        ))
        self.runtime.chat(ChatRequest(
            session_id="session-b",
            mensagem="Minha carteirinha é 9999 8888 7777 6666",
        ))

        state_a = self.runtime.get_session_state("session-a")
        state_b = self.runtime.get_session_state("session-b")

        self.assertEqual(state_a["turn_count"], 2)
        self.assertEqual(state_b["turn_count"], 1)
        self.assertEqual(state_a["candidate_carteirinha"], "1111222233334444")
        self.assertEqual(state_b["candidate_carteirinha"], "9999888877776666")
        self.assertEqual(len(state_a["messages"]), 4)
        self.assertEqual(len(state_b["messages"]), 2)

    def test_routes_explicit_handoffs_to_all_subagents(self) -> None:
        requests = [
            ChatRequest(session_id="routes", mensagem="Olá, preciso de ajuda"),
            ChatRequest(
                session_id="routes",
                mensagem="Minha carteirinha é 1111 2222 3333 4444",
            ),
            ChatRequest(
                session_id="routes",
                mensagem="",
                anexo=Anexo(
                    filename="recibo.pdf",
                    mime_type="application/pdf",
                    base64="dGVzdGU=",
                ),
            ),
            ChatRequest(session_id="routes", mensagem="Qual é o prazo?"),
        ]
        responses = [self.runtime.chat(request) for request in requests]
        state = self.runtime.get_session_state("routes")
        targets = {record.target for record in state["handoff_history"]}

        self.assertIn(AgentName.TRIAGEM, targets)
        self.assertIn(AgentName.DOCUMENTO, targets)
        self.assertIn(AgentName.NORMAS, targets)
        self.assertEqual(len(state["attachments"]), 1)
        self.assertTrue(all(len(response.resposta) >= 20 for response in responses))
        self.assertTrue(all(response.decisao is None for response in responses))

    def test_reset_removes_all_checkpointed_sessions(self) -> None:
        self.runtime.chat(ChatRequest(session_id="one", mensagem="Olá"))
        self.runtime.chat(ChatRequest(session_id="two", mensagem="Olá"))

        self.assertEqual(self.runtime.reset(), 2)
        self.assertEqual(self.runtime.session_count, 0)
        self.assertEqual(self.runtime.get_session_state("one"), {})
        self.assertEqual(self.runtime.get_session_state("two"), {})


class ApiContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.llm_patcher = patch(
            "app.agents.supervisor.graph.criar_llm",
            return_value=_FakeLLM(),
        )
        self.mcp_patcher = patch(
            "app.agents.triagem.agent.MCPOperadoraClient",
            return_value=_UnavailableMCP(),
        )
        self.llm_patcher.start()
        self.mcp_patcher.start()
        self.client = TestClient(app)
        self.client.post("/reset")

    def tearDown(self) -> None:
        self.client.post("/reset")
        self.mcp_patcher.stop()
        self.llm_patcher.stop()

    def test_health_chat_and_reset_contracts(self) -> None:
        health = self.client.get("/health")
        chat = self.client.post("/chat", json={
            "session_id": "api-session",
            "mensagem": "Olá, preciso solicitar um reembolso",
        })
        reset = self.client.post("/reset")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(chat.status_code, 200)
        self.assertEqual(
            set(chat.json()),
            {
                "resposta",
                "categoria_documento",
                "decisao",
                "valor_solicitado_brl",
                "valor_reembolso_brl",
                "regras_aplicadas",
                "protocolo",
                "pendencias",
            },
        )
        self.assertIsNone(chat.json()["decisao"])
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["sessions_cleared"], 1)

    def test_rejects_blank_session_id(self) -> None:
        response = self.client.post("/chat", json={
            "session_id": "   ",
            "mensagem": "Olá",
        })
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
