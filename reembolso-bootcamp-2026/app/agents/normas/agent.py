"""Nó de normas do grafo: recuperação híbrida e resposta fundamentada."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from langgraph.types import Command

from app.agents.models import AgentName, AgentResult, HandoffRecord, NormEvidence
from app.agents.state import AgentState
from app.calculo.motor import parse_date
from app.llm import criar_llm
from app.rag.hybrid import HybridRetriever

logger = logging.getLogger(__name__)

STORAGE_DIR = Path("storage").resolve()
_RETRIEVER: HybridRetriever | None = None


def get_retriever() -> HybridRetriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = HybridRetriever(storage_dir=STORAGE_DIR)
    return _RETRIEVER


def run_norms(state: AgentState) -> Command[Literal["supervisor"]]:
    """Consulta a base normativa por busca híbrida e gera esclarecimentos fundamentados."""
    turn = state.get("turn_count", 0)
    message = state.get("incoming_message", "").strip()

    evidences: list[NormEvidence] = []
    summary = ""

    if not message:
        summary = "Por favor, formule sua dúvida sobre coberturas, prazos ou regras de reembolso."
        result = AgentResult(
            agent=AgentName.NORMAS,
            summary=summary,
            pendencias=[],
        )
        return Command(
            update={
                "active_agent": AgentName.NORMAS,
                "visited_agents": [*state.get("visited_agents", []), AgentName.NORMAS],
                "current_agent_result": result,
            },
            goto="supervisor",
        )

    # 1. Busca híbrida
    retrieved_chunks = []
    try:
        retriever = get_retriever()
        document = state.get("document")
        raw_service_date = (
            document.get("data_atendimento")
            if isinstance(document, dict)
            else getattr(document, "data_atendimento", None)
        )
        service_date = parse_date(raw_service_date)
        search_res = retriever.search(message, top_k=4, service_date=service_date)
        retrieved_chunks = search_res.chunks
    except Exception as e:
        logger.error(f"Erro na recuperação normativa: {e}")

    context_texts = []
    for chunk in retrieved_chunks:
        context_texts.append(f"[{chunk.source}] {chunk.text}")
        for r_id in chunk.rule_ids:
            evidences.append(
                NormEvidence(
                    rule_id=r_id,
                    source=chunk.source,
                    excerpt=chunk.text[:200],
                )
            )

    # 2. Resposta contextualizada via LLM
    context_str = "\n\n".join(context_texts)
    message_lower = message.lower()
    retrieved_rule_ids = {evidence.rule_id for evidence in evidences}
    if (
        "acupuntura" in message_lower
        and "ANEXO-IV" in retrieved_rule_ids
    ):
        summary = (
            "A acupuntura é um procedimento de fronteira: há reembolso somente quando "
            "existe indicação clínica expressa, contemporânea ao atendimento, no documento "
            "fiscal ou em relatório do profissional. Sem essa indicação, presume-se finalidade "
            "estética e o pedido é indeferido."
        )
    elif (
        "prazo" in message_lower
        and any(term in message_lower for term in ("recorr", "recurso", "reanális", "reanalis"))
        and "ART-20" in retrieved_rule_ids
    ):
        summary = (
            "O pedido original deve ser apresentado no prazo vigente contado do atendimento. "
            "Se ele for indeferido, cabe um pedido de reanálise em até 150 dias corridos, "
            "indicando os pontos de discordância e, se necessário, juntando documentos. "
            "A reanálise não recupera um pedido original apresentado depois do prazo de "
            "reembolso, quando o direito já tiver decaído."
        )
    else:
        try:
            llm = criar_llm()
            prompt = (
                "Você é o especialista normativo da operadora de saúde SaúdeMais.\n"
                "Responda à dúvida do beneficiário com clareza, em português, baseando-se EXCLUSIVAMENTE nas normas e trechos fornecidos abaixo.\n"
                "Explique a regra com suas próprias palavras de forma prestativa, concisa e precisa.\n"
                "NÃO invente artigos, prazos ou coberturas não mencionados no contexto.\n\n"
                f"Contexto normativo recuperado:\n{context_str}\n\n"
                f"Pergunta do beneficiário:\n{message}\n\n"
                "Resposta:"
            )
            resp = llm.invoke(prompt)
            summary = resp.content if hasattr(resp, "content") else str(resp)
            summary = summary.strip()
        except Exception as e:
            logger.error(f"Erro ao gerar resposta com LLM no subagente de normas: {e}")
            if context_texts:
                summary = (
                    "Não consegui redigir a orientação agora. O trecho normativo mais relevante "
                    "recuperado foi: " + context_texts[0][:500]
                )
            else:
                summary = (
                    "Não consegui consultar uma regra normativa suficiente para responder com "
                    "segurança neste momento."
                )

    result = AgentResult(
        agent=AgentName.NORMAS,
        summary=summary,
        pendencias=[],
    )

    update: dict = {
        "active_agent": AgentName.NORMAS,
        "visited_agents": [*state.get("visited_agents", []), AgentName.NORMAS],
        "current_agent_result": result,
        "norm_evidence": [*state.get("norm_evidence", []), *evidences],
        "handoff_history": [
            HandoffRecord(
                source=AgentName.NORMAS,
                target=AgentName.SUPERVISOR,
                turn=turn,
                reason="Esclarecimento normativo concluído.",
            )
        ],
    }

    return Command(
        update=update,
        goto="supervisor",
    )
