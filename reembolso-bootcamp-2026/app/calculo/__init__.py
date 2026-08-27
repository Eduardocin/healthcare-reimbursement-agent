"""Módulo de cálculo e regras normativas."""

from app.calculo.motor import (
    ALCADA_PADRAO,
    CalculoInput,
    CalculoOutput,
    VALOR_URS_2026,
    calcular_meses_adesao,
    calcular_reembolso,
    obter_aliquota_coparticipacao,
    obter_teto_procedimento,
    parse_date,
)

__all__ = [
    "ALCADA_PADRAO",
    "CalculoInput",
    "CalculoOutput",
    "VALOR_URS_2026",
    "calcular_meses_adesao",
    "calcular_reembolso",
    "obter_aliquota_coparticipacao",
    "obter_teto_procedimento",
    "parse_date",
]
