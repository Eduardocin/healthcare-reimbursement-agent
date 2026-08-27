"""Modelos internos compartilhados pelo supervisor e pelos subagentes."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.schemas import Anexo, Categoria


class AgentName(str, Enum):
    SUPERVISOR = "supervisor"
    TRIAGEM = "ag_triagem"
    DOCUMENTO = "ag_documento"
    NORMAS = "ag_normas"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationMessage(BaseModel):
    role: MessageRole
    content: str
    turn: int = Field(ge=1)


class StoredAttachment(BaseModel):
    """Anexo persistido para processamento atual ou em turno posterior."""

    filename: str
    mime_type: str
    base64: str
    received_turn: int = Field(ge=1)

    @classmethod
    def from_request(cls, attachment: Anexo, turn: int) -> StoredAttachment:
        return cls(
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            base64=attachment.base64,
            received_turn=turn,
        )


class BeneficiarySnapshot(BaseModel):
    """Dados da operadora; será preenchido exclusivamente pelo cliente MCP."""

    carteirinha: str
    plano: str | None = None
    data_adesao: str | None = None
    situacao_contrato: str | None = None
    sessoes_terapia_ano: int | None = None
    valor_reembolsado_ano: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class DocumentResult(BaseModel):
    categoria: Categoria | None = None
    valor_pago_brl: str | None = None
    data_atendimento: str | None = None
    codigo_procedimento: str | None = None
    descricao_procedimento: str | None = None
    campos_presentes: list[str] = Field(default_factory=list)
    campos_ausentes: list[str] = Field(default_factory=list)
    attachment_filename: str | None = None


class NormEvidence(BaseModel):
    rule_id: str
    source: str
    excerpt: str
    valid_on_service_date: bool | None = None


class PendingItem(BaseModel):
    code: str
    description: str
    source_agent: AgentName


class AgentResult(BaseModel):
    agent: AgentName
    summary: str
    pendencias: list[str] = Field(default_factory=list)


class HandoffRecord(BaseModel):
    source: AgentName
    target: AgentName
    turn: int = Field(ge=1)
    reason: str
