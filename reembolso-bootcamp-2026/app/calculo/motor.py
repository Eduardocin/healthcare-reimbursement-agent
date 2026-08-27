"""Motor de cálculo determinístico e regras normativas de reembolso.

Todas as operações financeiras utilizam Decimal com arredondamento ROUND_HALF_UP a duas casas decimais.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.calculo.tabela import get_procedure_rule
from app.schemas import Categoria, Decisao

# Constantes normativas
VALOR_URS_2026 = Decimal("95.10")
LIMITE_ANUAL_URS = Decimal("48")  # Art. 45
LIMITE_ANUAL_REAIS = (LIMITE_ANUAL_URS * VALOR_URS_2026).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # R$ 4.564,80
ALCADA_PADRAO = Decimal("5000.00")  # Art. 78, § 1º
ALCADA_HOSPITALAR = Decimal("7500.00")  # Art. 78, § 1º
PRAZO_MAXIMO_DIAS = 90  # Art. 12 (restabelecido pela Circular 09/2025)
CIRCULAR_11_EFFECTIVE = date(2026, 2, 1)
CIRCULAR_02_EFFECTIVE = date(2026, 4, 20)


@dataclass
class CalculoInput:
    categoria: Categoria
    valor_solicitado: Decimal
    data_atendimento: date
    data_adesao: date
    plano: str  # "PLENO" ou "ESSENCIAL"
    codigo_procedimento: str | None = None
    sessoes_anteriores_ano: int = 0
    total_reembolsado_ano: Decimal = Decimal("0.00")
    tem_relatorio_clinico: bool = False
    data_solicitacao: date | None = None
    situacao_contrato: str | None = None
    campos_presentes: tuple[str, ...] = ()
    campos_ausentes: tuple[str, ...] = ()


@dataclass
class CalculoOutput:
    decisao: Decisao
    valor_solicitado_brl: Decimal | None
    valor_reembolso_brl: Decimal | None
    regras_aplicadas: list[str] = field(default_factory=list)
    pendencias: list[str] = field(default_factory=list)
    escalar_analista: bool = False
    motivo_escalonamento: str | None = None
    teto_procedimento_brl: Decimal | None = None
    base_elegivel_brl: Decimal | None = None
    aliquota_coparticipacao: Decimal | None = None
    saldo_anual_brl: Decimal | None = None


def parse_date(date_val: str | date | datetime | None) -> date | None:
    if date_val is None:
        return None
    if isinstance(date_val, date) and not isinstance(date_val, datetime):
        return date_val
    if isinstance(date_val, datetime):
        return date_val.date()
    try:
        return datetime.strptime(str(date_val).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def calcular_meses_adesao(data_adesao: date, data_atendimento: date) -> int:
    """Calcula o tempo de adesão em meses na data do atendimento (Art. 44, § 1º)."""
    anos = data_atendimento.year - data_adesao.year
    meses = data_atendimento.month - data_adesao.month
    dias = data_atendimento.day - data_adesao.day
    total_meses = anos * 12 + meses
    if dias < 0:
        total_meses -= 1
    return max(0, total_meses)


def obter_aliquota_coparticipacao(
    plano: str,
    meses_adesao: int,
    data_atendimento: date | None = None,
) -> Decimal:
    """Retorna o percentual de coparticipação conforme Circular 02/2026, Art. 3º."""
    plano_norm = plano.strip().upper()
    revised = data_atendimento is None or data_atendimento >= CIRCULAR_02_EFFECTIVE
    if plano_norm == "PLENO":
        if meses_adesao <= 12:
            return Decimal("0.40" if revised else "0.30")
        elif meses_adesao <= 36:
            return Decimal("0.30" if revised else "0.20")
        else:
            return Decimal("0.20" if revised else "0.10")
    else:  # ESSENCIAL ou outros
        if meses_adesao <= 12:
            return Decimal("0.45" if revised else "0.35")
        elif meses_adesao <= 36:
            return Decimal("0.35" if revised else "0.25")
        else:
            return Decimal("0.25" if revised else "0.15")


def obter_teto_procedimento(
    categoria: Categoria,
    codigo_tuss: str | None,
    sessoes_acumuladas: int = 0,
    data_atendimento: date | None = None,
) -> tuple[Decimal | None, list[str]]:
    """Retorna (teto_em_reais, regras_do_teto)."""
    # Consulta médica (TUSS 10101012 ou categoria CONSULTA_MEDICA)
    if categoria == Categoria.CONSULTA_MEDICA or (codigo_tuss and codigo_tuss.startswith("101010")):
        # Art. 35: 5 URS
        teto = (Decimal("5") * VALOR_URS_2026).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return teto, ["ART-35", "ART-33"]

    # Sessão de psicoterapia (TUSS 50000462 ou categoria SESSAO_TERAPIA)
    if categoria == Categoria.SESSAO_TERAPIA or codigo_tuss == "50000462":
        reference = data_atendimento or CIRCULAR_02_EFFECTIVE
        continued = sessoes_acumuladas >= 8
        if reference >= CIRCULAR_02_EFFECTIVE:
            urs = Decimal("2.6" if continued else "2.8")
            circular = "CIRC-02-2026"
        elif reference >= CIRCULAR_11_EFFECTIVE:
            urs = Decimal("1.4" if continued else "1.6")
            circular = "CIRC-11-2026"
        else:
            urs = Decimal("1.0" if continued else "1.2")
            circular = None
        teto = (urs * VALOR_URS_2026).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rules = ["ART-41"]
        if circular:
            rules.append(circular)
        rules.append("ART-33")
        return teto, rules

    procedure = get_procedure_rule(codigo_tuss)
    if procedure is None or procedure.ceiling_brl is None:
        return None, []
    return procedure.ceiling_brl, list(procedure.rule_ids)


def calcular_reembolso(dados: CalculoInput) -> CalculoOutput:
    """Executa o cálculo determinístico de reembolso aplicando todas as regras normativas vigentes."""
    procedure = get_procedure_rule(dados.codigo_procedimento)

    # 1. Validação de Categoria Especial / Alçada (Art. 78)
    if dados.categoria == Categoria.MATERIAL_OPME or (
        procedure is not None and procedure.requires_human_review
    ):
        return CalculoOutput(
            decisao=Decisao.ESCALADO_ANALISTA,
            valor_solicitado_brl=dados.valor_solicitado,
            valor_reembolso_brl=None,
            regras_aplicadas=["ART-78"],
            escalar_analista=True,
            motivo_escalonamento="Despesa com OPME / Material especial requer análise humana.",
        )

    alçada = (
        ALCADA_HOSPITALAR
        if procedure is not None and "hospitalar" in procedure.description.lower()
        else ALCADA_PADRAO
    )
    if dados.valor_solicitado > alçada:
        return CalculoOutput(
            decisao=Decisao.ESCALADO_ANALISTA,
            valor_solicitado_brl=dados.valor_solicitado,
            valor_reembolso_brl=None,
            regras_aplicadas=["ART-78"],
            escalar_analista=True,
            motivo_escalonamento=f"Valor solicitado (R$ {dados.valor_solicitado:.2f}) excede a alçada da análise automatizada (R$ {alçada:.2f}).",
        )

    if dados.categoria == Categoria.DESPESA_NAO_COBERTA:
        return CalculoOutput(
            decisao=Decisao.NEGADO,
            valor_solicitado_brl=dados.valor_solicitado,
            valor_reembolso_brl=Decimal("0.00"),
            regras_aplicadas=["ANEXO-IV"],
            pendencias=[],
        )

    if dados.categoria == Categoria.INVALIDO:
        return CalculoOutput(
            decisao=Decisao.PENDENTE_DOCUMENTO,
            valor_solicitado_brl=None,
            valor_reembolso_brl=None,
            regras_aplicadas=["ART-76"],
            pendencias=["Apresentar documento fiscal válido de despesa assistencial."],
        )

    situacao = (dados.situacao_contrato or "").strip().upper()
    if situacao and situacao not in {"ATIVO", "ATIVA", "REGULAR"}:
        return CalculoOutput(
            decisao=Decisao.NEGADO,
            valor_solicitado_brl=dados.valor_solicitado,
            valor_reembolso_brl=Decimal("0.00"),
            regras_aplicadas=["ART-30"],
        )

    # 2. Verificação de Prazo (Art. 12 + Circular 09/2025: 90 dias)
    if dados.data_solicitacao is not None:
        dias_passados = (dados.data_solicitacao - dados.data_atendimento).days
        if dias_passados > PRAZO_MAXIMO_DIAS:
            return CalculoOutput(
                decisao=Decisao.NEGADO,
                valor_solicitado_brl=dados.valor_solicitado,
                valor_reembolso_brl=Decimal("0.00"),
                regras_aplicadas=["ART-12", "CIRC-09-2025"],
                pendencias=[],
            )

    waiting_periods = {
        Categoria.CONSULTA_MEDICA: (15, "ART-22"),
        Categoria.EXAME_DIAGNOSTICO: (60, "ART-23"),
        Categoria.SESSAO_TERAPIA: (150, "ART-24"),
    }
    waiting_period = waiting_periods.get(dados.categoria)
    if waiting_period is not None:
        elapsed_days = (dados.data_atendimento - dados.data_adesao).days
        required_days, rule_id = waiting_period
        if elapsed_days < required_days:
            return CalculoOutput(
                decisao=Decisao.NEGADO,
                valor_solicitado_brl=dados.valor_solicitado,
                valor_reembolso_brl=Decimal("0.00"),
                regras_aplicadas=["ART-21", rule_id],
            )

    if dados.categoria == Categoria.SESSAO_TERAPIA and dados.sessoes_anteriores_ano >= 40:
        return CalculoOutput(
            decisao=Decisao.NEGADO,
            valor_solicitado_brl=dados.valor_solicitado,
            valor_reembolso_brl=Decimal("0.00"),
            regras_aplicadas=["ART-40"],
        )

    campos_ausentes = [
        campo
        for campo in dict.fromkeys(dados.campos_ausentes)
        if campo != "numero_sessao"
    ]
    campos_presentes = set(dados.campos_presentes)
    if procedure is not None:
        if procedure.requires_medical_order and "pedido_medico" not in campos_presentes:
            campos_ausentes.append("pedido_medico")
        if (
            procedure.requires_clinical_indication
            and "indicacao_clinica" not in campos_presentes
        ):
            campos_ausentes.append("indicacao_clinica")
    if campos_ausentes:
        descricoes = ", ".join(dict.fromkeys(campos_ausentes))
        return CalculoOutput(
            decisao=Decisao.PENDENTE_DOCUMENTO,
            valor_solicitado_brl=dados.valor_solicitado,
            valor_reembolso_brl=None,
            regras_aplicadas=["ART-18", "ART-73", "NT-02"],
            pendencias=[f"Complementar os seguintes campos/documentos: {descricoes}."],
        )

    # 3. Verificação de Exigência de Relatório Clínico (Art. 73, § 3º e Circular 02/2026, Art. 14)
    # Exigido se: a sessão for a 5ª ou posterior no ano, OU o valor exceder em > 75% o teto
    regras: list[str] = []
    sessao_atual = dados.sessoes_anteriores_ano + 1
    teto_procedimento, regras_teto = obter_teto_procedimento(
        dados.categoria,
        dados.codigo_procedimento,
        sessoes_acumuladas=dados.sessoes_anteriores_ano,
        data_atendimento=dados.data_atendimento,
    )

    if teto_procedimento is None:
        return CalculoOutput(
            decisao=Decisao.PENDENTE_DOCUMENTO,
            valor_solicitado_brl=dados.valor_solicitado,
            valor_reembolso_brl=None,
            regras_aplicadas=["ART-34", "NT-02"],
            pendencias=[
                "Não foi possível localizar o procedimento na Tabela URS; "
                "é necessário confirmar a descrição ou o código TUSS."
            ],
        )

    precisa_relatorio = False
    if dados.categoria == Categoria.SESSAO_TERAPIA:
        report_threshold = 5 if dados.data_atendimento >= CIRCULAR_02_EFFECTIVE else 10
        if sessao_atual >= report_threshold:
            precisa_relatorio = True
        elif dados.valor_solicitado > (teto_procedimento * Decimal("1.75")):
            precisa_relatorio = True

    if precisa_relatorio and not dados.tem_relatorio_clinico:
        return CalculoOutput(
            decisao=Decisao.PENDENTE_DOCUMENTO,
            valor_solicitado_brl=dados.valor_solicitado,
            valor_reembolso_brl=None,
            regras_aplicadas=[
                "ART-73",
                (
                    "CIRC-02-2026"
                    if dados.data_atendimento >= CIRCULAR_02_EFFECTIVE
                    else "CIRC-11-2026"
                ),
                "NT-02",
            ],
            pendencias=[
                f"Apresentação de relatório clínico circunstanciado para a {sessao_atual}ª sessão de psicoterapia do ano civil."
            ],
        )

    # 4. Ordem de Apuração Financeira (Arts. 43, 44, 45, 47)
    # 4.1 Teto (Art. 43)
    teto_cortou = dados.valor_solicitado > teto_procedimento
    base_elegivel = min(dados.valor_solicitado, teto_procedimento)
    regras.extend(regras_teto)

    if precisa_relatorio:
        # Se relatório foi exigido e apresentado, registra ART-73
        if "ART-73" not in regras:
            regras.insert(regras.index("ART-33") if "ART-33" in regras else 0, "ART-73")

    regras.append("ART-43")

    # 4.2 Coparticipação (Art. 44)
    meses_adesao = calcular_meses_adesao(dados.data_adesao, dados.data_atendimento)
    aliquota_copart = obter_aliquota_coparticipacao(
        dados.plano,
        meses_adesao,
        dados.data_atendimento,
    )
    fator_reembolso = Decimal("1.00") - aliquota_copart
    valor_apos_copart = base_elegivel * fator_reembolso
    regras.append("ART-44")

    # 4.3 Limite Anual (Art. 45)
    saldo_anual = max(Decimal("0.00"), LIMITE_ANUAL_REAIS - dados.total_reembolsado_ano)
    limite_anual_cortou = False
    if valor_apos_copart > saldo_anual:
        valor_final = saldo_anual
        limite_anual_cortou = True
        regras.append("ART-45")
    else:
        valor_final = valor_apos_copart

    # 4.4 Arredondamento (Art. 47)
    valor_arredondado = valor_final.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    regras.append("ART-47")

    # 4.5 Código TUSS (se houver)
    if dados.codigo_procedimento:
        regras.append(f"TUSS-{dados.codigo_procedimento}")

    # 5. Definição da Decisão
    if limite_anual_cortou or teto_cortou:
        decisao = Decisao.APROVADO_PARCIAL
    else:
        # Quando apenas a coparticipação incidiu, o reembolso é APROVADO
        decisao = Decisao.APROVADO

    # Remove duplicados preservando a ordem
    regras_unicas: list[str] = []
    for r in regras:
        if r not in regras_unicas:
            regras_unicas.append(r)

    return CalculoOutput(
        decisao=decisao,
        valor_solicitado_brl=dados.valor_solicitado,
        valor_reembolso_brl=valor_arredondado,
        regras_aplicadas=regras_unicas,
        pendencias=[],
        teto_procedimento_brl=teto_procedimento,
        base_elegivel_brl=base_elegivel,
        aliquota_coparticipacao=aliquota_copart,
        saldo_anual_brl=saldo_anual,
    )
