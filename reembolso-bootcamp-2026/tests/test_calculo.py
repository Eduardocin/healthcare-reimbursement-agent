"""Testes unitários para o motor de cálculo determinístico e regras de negócio."""

from datetime import date
from decimal import Decimal

from app.calculo.motor import (
    CalculoInput,
    calcular_reembolso,
    obter_aliquota_coparticipacao,
)
from app.schemas import Categoria, Decisao


def test_caso_1_consulta_aprovada():
    """Valida o cálculo do Caso 1: Consulta médica, R$ 240,00 -> R$ 144,00 (Plano Pleno, 7 meses)."""
    inp = CalculoInput(
        categoria=Categoria.CONSULTA_MEDICA,
        valor_solicitado=Decimal("240.00"),
        data_atendimento=date(2026, 4, 30),
        data_adesao=date(2025, 9, 1),
        plano="Pleno",
        codigo_procedimento="10101012",
    )
    res = calcular_reembolso(inp)
    assert res.decisao == Decisao.APROVADO
    assert res.valor_solicitado_brl == Decimal("240.00")
    assert res.valor_reembolso_brl == Decimal("144.00")
    assert res.regras_aplicadas == [
        "ART-35",
        "ART-33",
        "ART-43",
        "ART-44",
        "ART-47",
        "TUSS-10101012",
    ]


def test_caso_2_psicoterapia_sem_relatorio():
    """Valida a pendência de relatório clínico para 11ª sessão de psicoterapia."""
    inp = CalculoInput(
        categoria=Categoria.SESSAO_TERAPIA,
        valor_solicitado=Decimal("320.00"),
        data_atendimento=date(2026, 4, 25),
        data_adesao=date(2022, 3, 1),
        plano="Essencial",
        codigo_procedimento="50000462",
        sessoes_anteriores_ano=10,  # 11ª sessão
        total_reembolsado_ano=Decimal("4453.53"),
        tem_relatorio_clinico=False,
    )
    res = calcular_reembolso(inp)
    assert res.decisao == Decisao.PENDENTE_DOCUMENTO
    assert len(res.pendencias) > 0


def test_caso_2_psicoterapia_com_relatorio_saldo_anual():
    """Valida o Caso 2: 11ª sessão com relatório, limitado pelo saldo anual (R$ 320 -> R$ 111,27)."""
    inp = CalculoInput(
        categoria=Categoria.SESSAO_TERAPIA,
        valor_solicitado=Decimal("320.00"),
        data_atendimento=date(2026, 4, 25),
        data_adesao=date(2022, 3, 1),
        plano="Essencial",
        codigo_procedimento="50000462",
        sessoes_anteriores_ano=10,  # 11ª sessão
        total_reembolsado_ano=Decimal("4453.53"),
        tem_relatorio_clinico=True,
    )
    res = calcular_reembolso(inp)
    assert res.decisao == Decisao.APROVADO_PARCIAL
    assert res.valor_solicitado_brl == Decimal("320.00")
    assert res.valor_reembolso_brl == Decimal("111.27")
    assert res.regras_aplicadas == [
        "ART-41",
        "CIRC-02-2026",
        "ART-73",
        "ART-33",
        "ART-43",
        "ART-44",
        "ART-45",
        "ART-47",
        "TUSS-50000462",
    ]


def test_caso_3_opme_escalonamento():
    """Valida o Caso 3: OPME acima da alçada -> ESCALADO_ANALISTA sem valor apurado."""
    inp = CalculoInput(
        categoria=Categoria.MATERIAL_OPME,
        valor_solicitado=Decimal("9200.00"),
        data_atendimento=date(2026, 4, 30),
        data_adesao=date(2023, 1, 1),
        plano="Pleno",
        codigo_procedimento="70000039",
    )
    res = calcular_reembolso(inp)
    assert res.decisao == Decisao.ESCALADO_ANALISTA
    assert res.valor_solicitado_brl == Decimal("9200.00")
    assert res.valor_reembolso_brl is None
    assert res.escalar_analista is True
    assert res.regras_aplicadas == ["ART-78"]


def test_exame_usa_teto_especifico_da_tabela_urs():
    inp = CalculoInput(
        categoria=Categoria.EXAME_DIAGNOSTICO,
        valor_solicitado=Decimal("1200.00"),
        data_atendimento=date(2026, 5, 10),
        data_adesao=date(2020, 1, 1),
        plano="Pleno",
        codigo_procedimento="30201238",
        campos_presentes=("indicacao_clinica",),
    )
    res = calcular_reembolso(inp)
    assert res.decisao == Decisao.APROVADO_PARCIAL
    assert res.valor_reembolso_brl == Decimal("836.88")
    assert "TUSS-30201238" in res.regras_aplicadas
    assert "ART-60" in res.regras_aplicadas


def test_exame_com_indicacao_obrigatoria_ausente_fica_pendente():
    inp = CalculoInput(
        categoria=Categoria.EXAME_DIAGNOSTICO,
        valor_solicitado=Decimal("500.00"),
        data_atendimento=date(2026, 5, 10),
        data_adesao=date(2020, 1, 1),
        plano="Pleno",
        codigo_procedimento="30201238",
    )
    res = calcular_reembolso(inp)
    assert res.decisao == Decisao.PENDENTE_DOCUMENTO
    assert res.valor_reembolso_brl is None
    assert "indicacao_clinica" in res.pendencias[0]


def test_contrato_inativo_impede_reembolso():
    inp = CalculoInput(
        categoria=Categoria.CONSULTA_MEDICA,
        valor_solicitado=Decimal("200.00"),
        data_atendimento=date(2026, 5, 10),
        data_adesao=date(2020, 1, 1),
        plano="Pleno",
        codigo_procedimento="10101012",
        situacao_contrato="SUSPENSO",
    )
    res = calcular_reembolso(inp)
    assert res.decisao == Decisao.NEGADO
    assert res.regras_aplicadas == ["ART-30"]


def test_carencia_e_limite_de_sessoes_sao_aplicados():
    em_carencia = CalculoInput(
        categoria=Categoria.SESSAO_TERAPIA,
        valor_solicitado=Decimal("200.00"),
        data_atendimento=date(2026, 5, 10),
        data_adesao=date(2026, 4, 1),
        plano="Essencial",
        codigo_procedimento="50000462",
        tem_relatorio_clinico=True,
    )
    assert calcular_reembolso(em_carencia).regras_aplicadas == ["ART-21", "ART-24"]

    limite = CalculoInput(
        categoria=Categoria.SESSAO_TERAPIA,
        valor_solicitado=Decimal("200.00"),
        data_atendimento=date(2026, 5, 10),
        data_adesao=date(2020, 1, 1),
        plano="Essencial",
        codigo_procedimento="50000462",
        sessoes_anteriores_ano=40,
        tem_relatorio_clinico=True,
    )
    assert calcular_reembolso(limite).regras_aplicadas == ["ART-40"]


def test_vigencia_altera_teto_e_coparticipacao_da_psicoterapia():
    inp = CalculoInput(
        categoria=Categoria.SESSAO_TERAPIA,
        valor_solicitado=Decimal("200.00"),
        data_atendimento=date(2026, 3, 10),
        data_adesao=date(2020, 1, 1),
        plano="Pleno",
        codigo_procedimento="50000462",
        tem_relatorio_clinico=True,
    )
    res = calcular_reembolso(inp)
    assert res.valor_reembolso_brl == Decimal("136.94")
    assert "CIRC-11-2026" in res.regras_aplicadas
    assert "CIRC-02-2026" not in res.regras_aplicadas
