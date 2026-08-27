"""Nó de triagem do grafo: identificação cadastral, vínculo imutável e checagem de terceiros."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal

from langgraph.types import Command

from app.agents.models import (
    AgentName,
    AgentResult,
    BeneficiarySnapshot,
    HandoffRecord,
)
from app.agents.state import AgentState
from app.guardrails.privacy import check_third_party_request, extract_carteirinhas
from app.tools.mcp_client import MCPClientError, MCPOperadoraClient

logger = logging.getLogger(__name__)


def run_triage(state: AgentState) -> Command[Literal["supervisor"]]:
    """Valida a carteirinha no MCP, vincula a sessão e trata guardrail de terceiros."""
    turn = state.get("turn_count", 0)
    message = state.get("incoming_message", "")
    bound = state.get("bound_carteirinha")
    candidate = state.get("candidate_carteirinha")

    # 1. Extrai carteirinhas presentes na mensagem
    found_carteirinhas = extract_carteirinhas(message)
    extracted = found_carteirinhas[0] if found_carteirinhas else None
    target_carteirinha = extracted or candidate

    update: dict = {
        "active_agent": AgentName.TRIAGEM,
        "visited_agents": [*state.get("visited_agents", []), AgentName.TRIAGEM],
    }
    if target_carteirinha:
        update["candidate_carteirinha"] = target_carteirinha

    # 2. Se já tem carteirinha vinculada, verifica guardrail de terceiro
    if bound:
        is_third, third_c = check_third_party_request(message, bound)
        if is_third or (extracted and extracted != bound):
            update["third_party_detected"] = True
            summary = (
                "Não é permitido realizar consultas ou solicitações referentes a terceiros "
                "(outras carteirinhas ou dependentes). O atendimento permanece vinculado "
                "ao titular da sessão."
            )
            result = AgentResult(
                agent=AgentName.TRIAGEM,
                summary=summary,
                pendencias=[],
            )
            update["current_agent_result"] = result
            update["handoff_history"] = [
                HandoffRecord(
                    source=AgentName.TRIAGEM,
                    target=AgentName.SUPERVISOR,
                    turn=turn,
                    reason="Tentativa de consulta a terceiro detectada e bloqueada.",
                )
            ]
            return Command(update=update, goto="supervisor")
        else:
            update["third_party_detected"] = False

    # 3. Se não tem carteirinha vinculada, tenta validar via MCP
    if target_carteirinha and not bound:
        try:
            mcp_client = MCPOperadoraClient()
            benef_data = mcp_client.consultar_beneficiario_sync(target_carteirinha)

            # Consulta histórico para apurar sessões e total reembolsado no ano
            total_reembolsado = benef_data.valor_reembolsado_ano
            sessoes_ano = benef_data.sessoes_terapia_ano
            try:
                hist_data = mcp_client.consultar_historico_sync(target_carteirinha)
                if hist_data.pedidos:
                    soma_hist = Decimal("0.00")
                    count_terapia = 0
                    max_numero_sessao = 0
                    for ped in hist_data.pedidos:
                        status = (ped.status or "").upper()
                        concluido = not status or status in {
                            "PAGO", "APROVADO", "APROVADO_PARCIAL", "REEMBOLSADO"
                        }
                        if concluido and ped.valor_reembolsado_brl is not None:
                            soma_hist += ped.valor_reembolsado_brl
                        if concluido and (
                            ped.categoria == "SESSAO_TERAPIA"
                            or ped.procedimento == "50000462"
                        ):
                            count_terapia += 1
                            max_numero_sessao = max(
                                max_numero_sessao,
                                ped.numero_sessao or 0,
                            )
                    if soma_hist > total_reembolsado:
                        total_reembolsado = soma_hist
                    # O histórico é a fonte autorizada para a quantidade já
                    # realizada; o campo cadastral serve apenas como fallback.
                    sessoes_ano = max(max_numero_sessao, count_terapia)
            except MCPClientError as e:
                logger.warning(f"Histórico não disponível para {target_carteirinha}: {e}")

            snapshot = BeneficiarySnapshot(
                carteirinha=benef_data.carteirinha,
                plano=benef_data.plano,
                data_adesao=benef_data.data_adesao,
                situacao_contrato=benef_data.situacao_contrato,
                sessoes_terapia_ano=sessoes_ano,
                valor_reembolsado_ano=str(total_reembolsado),
                raw=benef_data.raw,
            )
            update["bound_carteirinha"] = benef_data.carteirinha
            update["candidate_carteirinha"] = benef_data.carteirinha
            update["beneficiary"] = snapshot

            situacao = benef_data.situacao_contrato or "Ativo"
            summary = (
                f"Localizei o seu cadastro na operadora: Plano {benef_data.plano}, "
                f"situação {situacao}. "
                "Sua carteirinha foi vinculada a este atendimento com sucesso, que ficará "
                "restrito aos dados do próprio titular."
            )
            result = AgentResult(
                agent=AgentName.TRIAGEM,
                summary=summary,
                pendencias=[],
            )
        except MCPClientError as err:
            logger.warning(f"Consulta MCP não realizada ou falhou para {target_carteirinha}: {err}")
            summary = (
                "Recebi a identificação, mas não consegui validar agora os dados cadastrais "
                "junto à operadora. Não tomarei uma decisão até concluir essa consulta."
            )
            result = AgentResult(
                agent=AgentName.TRIAGEM,
                summary=summary,
                pendencias=["Validar os dados cadastrais junto à operadora."],
            )
    elif bound:
        summary = "Cadastro já validado e vinculado ao titular da sessão."
        result = AgentResult(
            agent=AgentName.TRIAGEM,
            summary=summary,
            pendencias=[],
        )
    else:
        summary = (
            "O reembolso funciona assim: você envia o comprovante fiscal da despesa médica (nota ou recibo), "
            "consultamos as normas do seu plano e calculamos o valor de devolução conforme a tabela da operadora. "
            "Para iniciar, por favor informe o número da sua carteirinha SaúdeMais (16 dígitos)."
        )
        result = AgentResult(
            agent=AgentName.TRIAGEM,
            summary=summary,
            pendencias=["Informar a carteirinha do beneficiário desta sessão."],
        )

    update["current_agent_result"] = result
    update["handoff_history"] = [
        HandoffRecord(
            source=AgentName.TRIAGEM,
            target=AgentName.SUPERVISOR,
            turn=turn,
            reason="Triagem cadastral executada.",
        )
    ]
    return Command(update=update, goto="supervisor")
