"""Fachada thread-safe para o grafo conversacional."""

from __future__ import annotations

from threading import RLock
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.agents.supervisor.graph import build_graph
from app.schemas import ChatRequest, ChatResponse


class AgentRuntime:
    def __init__(self) -> None:
        serializer = JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("app.agents.models", "AgentName"),
                ("app.agents.models", "AgentResult"),
                ("app.agents.models", "BeneficiarySnapshot"),
                ("app.agents.models", "ConversationMessage"),
                ("app.agents.models", "DocumentResult"),
                ("app.agents.models", "HandoffRecord"),
                ("app.agents.models", "MessageRole"),
                ("app.agents.models", "NormEvidence"),
                ("app.agents.models", "PendingItem"),
                ("app.agents.models", "StoredAttachment"),
                ("app.schemas", "Anexo"),
                ("app.schemas", "Categoria"),
                ("app.schemas", "ChatResponse"),
                ("app.schemas", "Decisao"),
            ]
        )
        self._checkpointer = InMemorySaver(serde=serializer)
        self._graph = build_graph(self._checkpointer)
        self._session_ids: set[str] = set()
        self._lock = RLock()

    @staticmethod
    def _config(session_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": session_id}}

    def chat(self, request: ChatRequest) -> ChatResponse:
        session_id = request.session_id.strip()
        if not session_id:
            raise ValueError("session_id não pode ser vazio")

        with self._lock:
            # Registra antes da execução para que /reset também remova um
            # checkpoint parcial caso algum nó falhe no meio do turno.
            self._session_ids.add(session_id)
            result = self._graph.invoke(
                {
                    "session_id": session_id,
                    "incoming_message": request.mensagem,
                    "incoming_attachment": request.anexo,
                },
                config=self._config(session_id),
            )

        return ChatResponse.model_validate(result["response"])

    def reset(self) -> int:
        with self._lock:
            session_ids = tuple(self._session_ids)
            for session_id in session_ids:
                self._checkpointer.delete_thread(session_id)
            self._session_ids.clear()
        return len(session_ids)

    def get_session_state(self, session_id: str) -> dict[str, Any]:
        """Leitura diagnóstica usada pelos testes focados da arquitetura."""
        with self._lock:
            snapshot = self._graph.get_state(self._config(session_id))
        return dict(snapshot.values) if snapshot.values else {}

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._session_ids)


runtime = AgentRuntime()
