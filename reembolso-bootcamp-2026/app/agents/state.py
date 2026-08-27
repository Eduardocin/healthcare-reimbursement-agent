"""Estado persistente e contratos de entrada/saída do grafo."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from app.agents.models import (
    AgentName,
    AgentResult,
    BeneficiarySnapshot,
    ConversationMessage,
    DocumentResult,
    HandoffRecord,
    NormEvidence,
    PendingItem,
    StoredAttachment,
)
from app.schemas import Anexo, ChatResponse


class TurnInput(TypedDict):
    session_id: str
    incoming_message: str
    incoming_attachment: Anexo | None


class GraphOutput(TypedDict):
    response: ChatResponse


class AgentState(TypedDict, total=False):
    # Identidade e memória durável da sessão.
    session_id: str
    turn_count: int
    messages: Annotated[list[ConversationMessage], operator.add]
    attachments: Annotated[list[StoredAttachment], operator.add]
    handoff_history: Annotated[list[HandoffRecord], operator.add]

    # Entrada do turno atual, sobrescrita a cada invocação.
    incoming_message: str
    incoming_attachment: Anexo | None

    # Dados acumulados pelas fontes autorizadas.
    candidate_carteirinha: str | None
    bound_carteirinha: str | None
    beneficiary: BeneficiarySnapshot | None
    document: DocumentResult | None
    last_document_summary: str | None
    document_issue: str | None
    has_clinical_report: bool
    third_party_detected: bool
    norm_evidence: list[NormEvidence]
    pending_items: list[PendingItem]
    regras_finais: list[str]
    protocolo_aberto: str | None

    # Controle transitório do turno.
    active_agent: AgentName | None
    visited_agents: list[AgentName]
    current_agent_result: AgentResult | None
    response: ChatResponse
