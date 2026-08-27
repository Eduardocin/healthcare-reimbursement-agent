"""Nó de documento do grafo com extração, OCR e classificação."""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from langgraph.types import Command

from app.agents.documento.extractor import extract_document_info
from app.agents.models import AgentName, AgentResult, DocumentResult, HandoffRecord
from app.agents.state import AgentState
from app.schemas import Categoria

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/tiff",
    "image/bmp",
}


def run_document(state: AgentState) -> Command[Literal["supervisor"]]:
    """Decodifica, extrai e classifica o anexo recebido."""
    turn = state.get("turn_count", 0)
    attachment = state.get("incoming_attachment")

    if attachment is None:
        result = AgentResult(
            agent=AgentName.DOCUMENTO,
            summary="Nenhum anexo novo foi enviado neste turno.",
            pendencias=[],
        )
        return Command(
            update={
                "active_agent": AgentName.DOCUMENTO,
                "visited_agents": [*state.get("visited_agents", []), AgentName.DOCUMENTO],
                "current_agent_result": result,
            },
            goto="supervisor",
        )

    # 1. Decodifica Base64
    try:
        encoded = "".join(attachment.base64.split())
        file_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        result = AgentResult(
            agent=AgentName.DOCUMENTO,
            summary=(
                f"O arquivo {attachment.filename} não pôde ser decodificado corretamente. "
                "Por favor, envie o documento em formato válido (PDF, imagem ou DOCX)."
            ),
            pendencias=["Envio de arquivo em formato legível e decodificável."],
        )
        return Command(
            update={
                "active_agent": AgentName.DOCUMENTO,
                "visited_agents": [*state.get("visited_agents", []), AgentName.DOCUMENTO],
                "current_agent_result": result,
                "last_document_summary": result.summary,
                "document_issue": result.summary,
                "handoff_history": [
                    HandoffRecord(
                        source=AgentName.DOCUMENTO,
                        target=AgentName.SUPERVISOR,
                        turn=turn,
                        reason="Falha de decodificação do anexo.",
                    )
                ],
            },
            goto="supervisor",
        )

    mime_type = attachment.mime_type.lower().split(";", 1)[0].strip()
    if mime_type not in SUPPORTED_MIME_TYPES or not file_bytes or len(file_bytes) > MAX_ATTACHMENT_BYTES:
        reason = (
            "O anexo deve ser um PDF, DOCX ou imagem legível de até 10 MB."
        )
        result = AgentResult(
            agent=AgentName.DOCUMENTO,
            summary=reason,
            pendencias=[reason],
        )
        return Command(
            update={
                "active_agent": AgentName.DOCUMENTO,
                "visited_agents": [*state.get("visited_agents", []), AgentName.DOCUMENTO],
                "current_agent_result": result,
                "last_document_summary": result.summary,
                "document_issue": result.summary,
                "handoff_history": [
                    HandoffRecord(
                        source=AgentName.DOCUMENTO,
                        target=AgentName.SUPERVISOR,
                        turn=turn,
                        reason="Tipo, tamanho ou conteúdo do anexo inválido.",
                    )
                ],
            },
            goto="supervisor",
        )

    # 2. Extrai e classifica dados do documento
    extracted = extract_document_info(file_bytes, attachment.filename, attachment.mime_type)
    missing_fields = list(extracted.campos_ausentes)
    if extracted.categoria == Categoria.SESSAO_TERAPIA and state.get("beneficiary"):
        missing_fields = [field for field in missing_fields if field != "numero_sessao"]

    update: dict = {
        "active_agent": AgentName.DOCUMENTO,
        "visited_agents": [*state.get("visited_agents", []), AgentName.DOCUMENTO],
    }

    # 3. Trata de acordo com a categoria
    if extracted.categoria == Categoria.INVALIDO:
        summary = (
            f"O arquivo '{attachment.filename}' enviado não é um documento fiscal de despesa assistencial "
            "(como nota fiscal de serviços médicos ou recibo de prestação de serviços de saúde). "
            "Para que possamos analisar sua solicitação de reembolso, por favor envie o comprovante fiscal assistencial correspondente."
        )
        pendencias = ["Apresentar documento fiscal válido de despesa assistencial."]
        result = AgentResult(
            agent=AgentName.DOCUMENTO,
            summary=summary,
            pendencias=pendencias,
        )
    elif extracted.categoria == Categoria.RELATORIO_CLINICO:
        current_document = state.get("document")
        current_category = (
            current_document.categoria
            if isinstance(current_document, DocumentResult)
            else current_document.get("categoria") if isinstance(current_document, dict) else None
        )
        complements_therapy = bool(
            current_document and current_category == Categoria.SESSAO_TERAPIA
        )
        if complements_therapy:
            update["has_clinical_report"] = True
            summary = (
                f"Recebi o relatório clínico '{attachment.filename}'. O documento foi anexado "
                "ao pedido de terapia já existente."
            )
        else:
            update["document"] = DocumentResult(
                categoria=extracted.categoria,
                valor_pago_brl=(
                    str(extracted.valor_pago_brl) if extracted.valor_pago_brl else None
                ),
                data_atendimento=extracted.data_atendimento,
                codigo_procedimento=extracted.codigo_procedimento,
                descricao_procedimento=extracted.descricao_procedimento,
                campos_presentes=extracted.campos_presentes,
                campos_ausentes=missing_fields,
                attachment_filename=attachment.filename,
            )
            summary = (
                f"Recebi o relatório clínico '{attachment.filename}' como documento principal "
                "deste pedido."
            )
        result = AgentResult(
            agent=AgentName.DOCUMENTO,
            summary=summary,
            pendencias=missing_fields,
        )
    else:
        doc_res = DocumentResult(
            categoria=extracted.categoria,
            valor_pago_brl=str(extracted.valor_pago_brl) if extracted.valor_pago_brl else None,
            data_atendimento=extracted.data_atendimento,
            codigo_procedimento=extracted.codigo_procedimento,
            descricao_procedimento=extracted.descricao_procedimento,
            campos_presentes=extracted.campos_presentes,
            campos_ausentes=missing_fields,
            attachment_filename=attachment.filename,
        )
        update["document"] = doc_res
        val_txt = f" de R$ {extracted.valor_pago_brl:.2f}" if extracted.valor_pago_brl else ""
        missing_txt = (
            " Ainda preciso dos seguintes campos ou documentos: "
            + ", ".join(missing_fields)
            + "."
            if missing_fields
            else ""
        )
        summary = (
            f"Recebi o documento '{attachment.filename}' referente a {extracted.categoria.value.lower().replace('_', ' ')}{val_txt}. "
            "Os dados foram extraídos e integrados ao seu pedido de reembolso."
            f"{missing_txt}"
        )
        result = AgentResult(
            agent=AgentName.DOCUMENTO,
            summary=summary,
            pendencias=missing_fields,
        )

    update["current_agent_result"] = result
    update["last_document_summary"] = summary
    update["document_issue"] = summary if extracted.categoria == Categoria.INVALIDO else None
    update["handoff_history"] = [
        HandoffRecord(
            source=AgentName.DOCUMENTO,
            target=AgentName.SUPERVISOR,
            turn=turn,
            reason="Processamento do anexo concluído.",
        )
    ]

    return Command(
        update=update,
        goto="supervisor",
    )
