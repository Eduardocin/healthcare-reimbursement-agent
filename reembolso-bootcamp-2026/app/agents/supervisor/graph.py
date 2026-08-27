"""Grafo do supervisor e orquestração dos subagentes de reembolso."""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from langgraph.graph import START, StateGraph
from langgraph.types import Command

from app.agents.documento.agent import run_document
from app.agents.models import (
    AgentName,
    BeneficiarySnapshot,
    ConversationMessage,
    DocumentResult,
    HandoffRecord,
    MessageRole,
    StoredAttachment,
)
from app.agents.normas.agent import run_norms
from app.agents.state import AgentState, GraphOutput, TurnInput
from app.agents.triagem.agent import run_triage
from app.calculo.motor import CalculoInput, calcular_reembolso, parse_date
from app.guardrails.privacy import (
    check_third_party_request,
    extract_carteirinhas,
    sanitize_text,
)
from app.llm import criar_llm
from app.schemas import Anexo, Categoria, ChatResponse, Decisao
from app.tools.mcp_client import MCPClientError, MCPOperadoraClient

logger = logging.getLogger(__name__)

NORMATIVE_TERMS = frozenset(
    {
        "cobertura", "cobre", "prazo", "regra", "recurso", "recorrer",
        "reembolso", "reembolsado", "valor", "quanto", "limite", "teto",
        "acupuntura", "psicoterapia", "terapia", "relatorio", "relatório",
        "coparticipacao", "coparticipação", "por que", "porque",
    }
)

HandoffDestination = Literal["ag_triagem", "ag_documento", "ag_normas"]
SupervisorDestination = Literal["ag_triagem", "ag_documento", "ag_normas", "__end__"]
END_NODE: Literal["__end__"] = "__end__"


def prepare_turn(state: AgentState) -> dict:
    """Abre um novo turno e prepara os campos para roteamento."""
    turn = state.get("turn_count", 0) + 1
    message = state.get("incoming_message", "")
    attachment_value = state.get("incoming_attachment")
    attachment = Anexo.model_validate(attachment_value) if attachment_value is not None else None

    update: dict = {
        "turn_count": turn,
        "messages": [ConversationMessage(role=MessageRole.USER, content=message, turn=turn)],
        "active_agent": None,
        "visited_agents": [],
        "current_agent_result": None,
        "third_party_detected": False,
        "last_document_summary": None,
    }
    if attachment is not None:
        update["attachments"] = [StoredAttachment.from_request(attachment, turn)]
    return update


def _select_agent(state: AgentState) -> tuple[HandoffDestination, str]:
    """Decide qual subagente deve tratar o turno atual."""
    visited = state.get("visited_agents", [])

    # 1. Anexo no turno atual -> Subagente de Documento
    if state.get("incoming_attachment") is not None and AgentName.DOCUMENTO not in visited:
        return "ag_documento", "O turno contém um anexo novo a ser processado."

    msg = state.get("incoming_message", "")
    found_carteirinhas = extract_carteirinhas(msg)
    bound = state.get("bound_carteirinha")
    candidate = state.get("candidate_carteirinha")

    # 2. Carteirinha informada explicitamente na mensagem -> Subagente de Triagem
    if found_carteirinhas and AgentName.TRIAGEM not in visited:
        return "ag_triagem", "Identificação cadastral, validação de carteirinha ou verificação de titularidade."

    # 3. Sem identificação, a triagem vem antes da análise normativa.
    if (not bound and not candidate) and AgentName.TRIAGEM not in visited:
        return "ag_triagem", "Solicitação inicial de identificação cadastral."

    # 4. Dúvidas normativas -> Subagente de Normas
    words = set(re.findall(r"\w+", msg.lower()))
    if (words & NORMATIVE_TERMS or "?" in msg) and AgentName.NORMAS not in visited:
        return "ag_normas", "A mensagem contém uma dúvida sobre regras, prazos ou coberturas."

    if AgentName.TRIAGEM not in visited:
        return "ag_triagem", "Triagem geral do atendimento."

    if AgentName.NORMAS not in visited:
        return "ag_normas", "Apoio normativo complementar."

    return "ag_triagem", "Conclusão do turno."


def _generate_conversational_response(
    state: Any,
    subagent_summary: str,
    calc_explanation: str | None = None,
    calc_output: Any | None = None,
) -> str:
    """Redige a resposta usando somente fatos consolidados neste atendimento."""
    message = state.get("incoming_message", "")
    prev_messages = state.get("messages", [])
    bound = state.get("bound_carteirinha")
    doc = state.get("document")
    benef = state.get("beneficiary")
    message_lower = message.lower()
    current_result = state.get("current_agent_result")
    result_agent = (
        current_result.get("agent")
        if isinstance(current_result, dict)
        else getattr(current_result, "agent", None)
    )
    document_summary = state.get("last_document_summary")
    document_issue = state.get("document_issue")

    def deterministic_response(*parts: str | None) -> str:
        unique = [part.strip() for part in parts if part and part.strip()]
        return sanitize_text(" ".join(dict.fromkeys(unique)), bound)

    if state.get("third_party_detected"):
        return deterministic_response(
            "Não posso consultar nem usar dados de cônjuge, dependente ou qualquer outra "
            "pessoa neste atendimento. O pedido atual continua vinculado somente ao titular "
            "da carteirinha validada.",
            calc_explanation,
        )

    if state.get("incoming_attachment") is not None and document_summary:
        return deterministic_response(document_summary, calc_explanation, subagent_summary)

    if document_issue and any(
        term in message_lower
        for term in ("não é", "nao e", "arquivo", "certo", "correto", "celular")
    ):
        return deterministic_response(
            document_issue,
            "Esse arquivo não serve para comprovar a despesa assistencial. O protocolo do "
            "pedido continua aberto aguardando o documento fiscal correto.",
        )

    if "data" in message_lower and any(
        term in message_lower for term in ("antes", "errad", "papel", "documento")
    ):
        return deterministic_response(
            "A análise usa a data registrada no documento enviado. Se a data do papel estiver "
            "correta, ela será mantida; se estiver errada, envie um documento retificado antes "
            "da conclusão do pedido.",
            calc_explanation,
        )

    if benef and any(term in message_lower for term in ("quantas", "não faço ideia", "nao faco ideia")):
        sessions = (
            benef.get("sessoes_terapia_ano")
            if isinstance(benef, dict)
            else benef.sessoes_terapia_ano
        )
        return deterministic_response(
            f"Você não precisa estimar: consultei o histórico da operadora, que registra "
            f"{sessions} sessões anteriores no ano; portanto, o recibo atual corresponde à "
            f"{sessions + 1}ª sessão.",
            calc_explanation,
        )

    if (
        calc_output is not None
        and calc_output.decisao == Decisao.PENDENTE_DOCUMENTO
        and "relat" in message_lower
    ):
        return deterministic_response(
            "O relatório clínico passa a ser obrigatório a partir do ponto previsto para o "
            "acompanhamento. Sem ele, o pedido fica pendente — não negado — e permanece aberto "
            "aguardando o documento.",
            calc_explanation,
        )

    if calc_explanation and any(
        term in message_lower
        for term in (
            "como fica", "quanto", "valor", "inteiro", "só isso", "so isso",
            "tem a ver", "por que", "porque", "agora sai", "sistema não calcula",
            "sistema nao calcula",
        )
    ):
        return deterministic_response(calc_explanation)

    normalized_result_agent = getattr(result_agent, "value", result_agent)
    if normalized_result_agent == AgentName.NORMAS.value:
        return deterministic_response(subagent_summary, calc_explanation)

    past_assistant_replies = [
        m.content for m in prev_messages if m.role == MessageRole.ASSISTANT
    ][-3:]
    history_ctx = "\n".join([f"- {r}" for r in past_assistant_replies]) if past_assistant_replies else "Nenhuma."

    facts = [f"Resultado do especialista deste turno: {subagent_summary}"]
    if calc_explanation:
        facts.append(f"Resultado determinístico do pedido: {calc_explanation}")
    if bound:
        facts.append("A carteirinha desta sessão já foi validada; não a solicite novamente.")
    if benef:
        sessoes = getattr(benef, "sessoes_terapia_ano", None)
        if sessoes is None and isinstance(benef, dict):
            sessoes = benef.get("sessoes_terapia_ano")
        if sessoes is not None:
            facts.append(f"O histórico da operadora registra {sessoes} sessões de terapia no ano.")
    if doc:
        category = doc.get("categoria") if isinstance(doc, dict) else doc.categoria
        amount = doc.get("valor_pago_brl") if isinstance(doc, dict) else doc.valor_pago_brl
        missing = doc.get("campos_ausentes", []) if isinstance(doc, dict) else doc.campos_ausentes
        category_text = getattr(category, "value", category) or "não confirmada"
        facts.append(f"Categoria documental confirmada: {category_text}.")
        if amount:
            facts.append(f"Valor do documento: R$ {Decimal(amount):.2f}.")
        if missing:
            facts.append("Campos documentais ainda ausentes: " + ", ".join(missing) + ".")
    if state.get("third_party_detected"):
        facts.append(
            "O pedido atual envolve terceiro: recuse a consulta, não confirme dados e oriente "
            "a abertura de atendimento próprio, preservando o pedido original desta sessão."
        )

    prompt = (
        "Você é o assistente virtual da operadora de saúde SaúdeMais para reembolso de despesas médicas.\n"
        "Responda em português claro, cordial e objetivo. Atenda primeiro ao que foi dito ou "
        "perguntado neste turno; depois solicite apenas o próximo dado realmente necessário.\n"
        "Use exclusivamente os fatos consolidados abaixo. Não complete lacunas, não invente "
        "valor, data, regra ou protocolo. Não reproduza CPF, CID, hipótese diagnóstica, quadro "
        "clínico ou carteirinha de terceiro.\n"
        "Não repita literalmente uma resposta anterior e escreva ao menos 20 caracteres úteis.\n\n"
        f"Fatos consolidados:\n- " + "\n- ".join(facts) + "\n\n"
        f"Respostas anteriores recentes:\n{history_ctx}\n\n"
        f"Mensagem atual do beneficiário:\n{message}\n\n"
        "Resposta final ao beneficiário:"
    )

    try:
        llm = criar_llm()
        resp = llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        cleaned = text.strip()
        if len(cleaned) >= 20:
            return sanitize_text(cleaned, bound)
    except Exception as e:
        logger.error(f"Erro ao gerar resposta conversacional via LLM: {e}")

    # Fallback contextualizado
    fallback = subagent_summary
    if calc_explanation and calc_explanation not in fallback:
        fallback += " " + calc_explanation
    return sanitize_text(fallback, bound)


def supervisor(state: AgentState) -> Command[SupervisorDestination]:
    """Orquestrador central do grafo."""
    result = state.get("current_agent_result")
    turn = state.get("turn_count", 0)

    # 1. Se ainda não executou nenhum subagente neste turno, despacha para o próximo
    if result is None:
        target, reason = _select_agent(state)
        return Command[SupervisorDestination](
            update={
                "active_agent": AgentName.SUPERVISOR,
                "handoff_history": [
                    HandoffRecord(
                        source=AgentName.SUPERVISOR,
                        target=AgentName(target),
                        turn=turn,
                        reason=reason,
                    )
                ],
            },
            goto=target,
        )

    visited = state.get("visited_agents", [])
    message = state.get("incoming_message", "")
    has_identity = bool(state.get("bound_carteirinha") or state.get("candidate_carteirinha"))
    needs_triage = (
        (not has_identity or bool(extract_carteirinhas(message)))
        and AgentName.TRIAGEM not in visited
    )
    words = set(re.findall(r"\w+", message.lower()))
    needs_norms = (
        has_identity
        and not state.get("third_party_detected", False)
        and (bool(words & NORMATIVE_TERMS) or "?" in message)
        and AgentName.NORMAS not in visited
    )
    if needs_triage or needs_norms:
        target: HandoffDestination = "ag_triagem" if needs_triage else "ag_normas"
        reason = (
            "O mesmo turno ainda exige validação cadastral."
            if needs_triage
            else "O mesmo turno também contém uma dúvida normativa."
        )
        return Command[SupervisorDestination](
            update={
                "active_agent": AgentName.SUPERVISOR,
                "handoff_history": [
                    HandoffRecord(
                        source=AgentName.SUPERVISOR,
                        target=AgentName(target),
                        turn=turn,
                        reason=reason,
                    )
                ],
                "current_agent_result": None,
            },
            goto=target,
        )

    # 2. Consolidação do turno e avaliação de cálculo
    bound = state.get("bound_carteirinha")
    benef_raw = state.get("beneficiary")
    doc_raw = state.get("document")
    has_report = state.get("has_clinical_report", False)
    third_party = state.get("third_party_detected", False)
    protocolo_aberto = state.get("protocolo_aberto")

    benef = (
        BeneficiarySnapshot.model_validate(benef_raw)
        if isinstance(benef_raw, dict)
        else benef_raw
    )
    doc = (
        DocumentResult.model_validate(doc_raw)
        if isinstance(doc_raw, dict)
        else doc_raw
    )

    calc_output = None
    calc_explanation = None

    if bound and benef and doc and doc.valor_pago_brl and doc.categoria:
        # Temos dados suficientes para executar o motor determinístico
        try:
            val_pago = Decimal(str(doc.valor_pago_brl))
            dt_atend = parse_date(doc.data_atendimento)
            dt_adesao = parse_date(benef.data_adesao)
            if dt_atend is None or dt_adesao is None or not benef.plano:
                missing = []
                if dt_atend is None:
                    missing.append("data do atendimento no documento")
                if dt_adesao is None or not benef.plano:
                    missing.append("dados de adesão e plano no cadastro da operadora")
                result.pendencias.extend(item for item in missing if item not in result.pendencias)
                raise ValueError("Dados insuficientes para calcular: " + ", ".join(missing))
            total_reemb = Decimal(str(benef.valor_reembolsado_ano or "0.00"))

            calc_in = CalculoInput(
                categoria=doc.categoria,
                valor_solicitado=val_pago,
                data_atendimento=dt_atend,
                data_adesao=dt_adesao,
                plano=benef.plano,
                codigo_procedimento=doc.codigo_procedimento,
                sessoes_anteriores_ano=benef.sessoes_terapia_ano or 0,
                total_reembolsado_ano=total_reemb,
                tem_relatorio_clinico=has_report,
                situacao_contrato=benef.situacao_contrato,
                campos_presentes=tuple(doc.campos_presentes),
                campos_ausentes=tuple(doc.campos_ausentes),
            )
            calc_output = calcular_reembolso(calc_in)

            # Se exigir escalonamento e ainda não tiver protocolo, abre via MCP
            if calc_output.escalar_analista and not protocolo_aberto:
                try:
                    mcp_client = MCPOperadoraClient()
                    payload = {
                        "categoria": doc.categoria.value,
                        "valor_solicitado": str(val_pago),
                        "procedimento": doc.codigo_procedimento,
                        "motivo": calc_output.motivo_escalonamento,
                    }
                    proto_res = mcp_client.abrir_protocolo_sync(bound, payload)
                    protocolo_aberto = proto_res.protocolo
                except MCPClientError as e:
                    logger.error(f"Erro ao abrir protocolo no MCP: {e}")
                    calc_output = None
                    calc_explanation = (
                        "Não foi possível abrir o protocolo obrigatório na operadora neste "
                        "momento. O pedido não foi marcado como escalado e deve ser tentado novamente."
                    )
                    result.pendencias.append("Abertura do protocolo na operadora.")

            if calc_output and calc_output.decisao == Decisao.ESCALADO_ANALISTA:
                calc_explanation = (
                    f"A sua solicitação foi encaminhada para análise humana da operadora sob o protocolo {protocolo_aberto}. "
                    "Por tratar-se de despesa acima da alçada ou de material especial/OPME, o valor de reembolso será definido após a conclusão da análise pelo analista."
                )
            elif calc_output and calc_output.decisao == Decisao.APROVADO:
                coparticipation = (calc_output.aliquota_coparticipacao or Decimal("0")) * 100
                calc_explanation = (
                    f"O reembolso foi APROVADO no valor de R$ {calc_output.valor_reembolso_brl:.2f}. "
                    f"O cálculo aplica primeiro o teto do procedimento (R$ {calc_output.teto_procedimento_brl:.2f}); "
                    f"a base elegível ficou em R$ {calc_output.base_elegivel_brl:.2f}. Depois incide "
                    f"a coparticipação de {coparticipation:.0f}%, definida pelo plano e pelo tempo "
                    "de adesão, razão pela qual o valor pago não retorna integralmente."
                )
            elif calc_output and calc_output.decisao == Decisao.APROVADO_PARCIAL:
                coparticipation = (calc_output.aliquota_coparticipacao or Decimal("0")) * 100
                calc_explanation = (
                    f"O reembolso foi APROVADO PARCIALMENTE no valor de R$ {calc_output.valor_reembolso_brl:.2f}. "
                    f"Primeiro foi aplicado o teto de R$ {calc_output.teto_procedimento_brl:.2f} e, "
                    f"depois, a coparticipação de {coparticipation:.0f}%. Como os reembolsos já "
                    f"pagos no ano reduziram o saldo anual disponível para R$ {calc_output.saldo_anual_brl:.2f}, "
                    "esse saldo limitou o valor final desta solicitação."
                )
            elif calc_output and calc_output.decisao == Decisao.PENDENTE_DOCUMENTO:
                calc_explanation = (
                    "O pedido está pendente e não possui valor apurado enquanto faltarem: "
                    + "; ".join(calc_output.pendencias)
                )
            elif calc_output and calc_output.decisao == Decisao.NEGADO:
                calc_explanation = (
                    "O pedido foi negado pelas regras aplicáveis ao caso, sem valor de reembolso."
                )
        except (FileNotFoundError, InvalidOperation, ValueError) as e:
            logger.error(f"Erro no cálculo determinístico: {e}")

    # 3. Montagem do ChatResponse
    # Injeta protocolo_aberto atualizado no estado para o contexto dinâmico do prompt
    state_for_response = dict(state)
    state_for_response["protocolo_aberto"] = protocolo_aberto
    resp_text = _generate_conversational_response(
        state_for_response,
        result.summary,
        calc_explanation,
        calc_output,
    )

    chat_resp = ChatResponse(
        resposta=resp_text,
        categoria_documento=doc.categoria if doc else None,
        decisao=Decisao.FORA_DE_ESCOPO if third_party else (calc_output.decisao if calc_output else None),
        valor_solicitado_brl=(
            calc_output.valor_solicitado_brl if calc_output and not third_party else None
        ),
        valor_reembolso_brl=calc_output.valor_reembolso_brl if (calc_output and not third_party) else None,
        regras_aplicadas=(calc_output.regras_aplicadas if calc_output and not third_party else []),
        protocolo=(
            protocolo_aberto
            if calc_output and calc_output.escalar_analista and not third_party
            else None
        ),
        pendencias=calc_output.pendencias if calc_output else result.pendencias,
    )

    update_dict: dict = {
        "active_agent": AgentName.SUPERVISOR,
        "protocolo_aberto": protocolo_aberto,
        "messages": [
            ConversationMessage(
                role=MessageRole.ASSISTANT,
                content=chat_resp.resposta,
                turn=turn,
            )
        ],
        "response": chat_resp,
    }

    return Command[SupervisorDestination](
        update=update_dict,
        goto=END_NODE,
    )


def build_graph(checkpointer):
    builder = StateGraph(AgentState, input_schema=TurnInput, output_schema=GraphOutput)
    builder.add_node("prepare_turn", prepare_turn)
    builder.add_node(AgentName.SUPERVISOR.value, supervisor)
    builder.add_node(AgentName.TRIAGEM.value, run_triage)
    builder.add_node(AgentName.DOCUMENTO.value, run_document)
    builder.add_node(AgentName.NORMAS.value, run_norms)

    builder.add_edge(START, "prepare_turn")
    builder.add_edge("prepare_turn", AgentName.SUPERVISOR.value)

    return builder.compile(checkpointer=checkpointer, name="reimbursement-supervisor")
