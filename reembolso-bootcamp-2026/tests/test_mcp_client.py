"""Testes unitários para os modelos e adaptadores do cliente MCP."""

from decimal import Decimal
from app.tools.mcp_client import BeneficiarioData, HistoricoData, HistoricoItem


def test_beneficiario_v1_parsing():
    raw_v1 = {
        "carteirinha": "1234567890123456",
        "nome": "João Silva",
        "cpf": "123.***.***-00",
        "plano": "ESSENCIAL",
        "data_adesao": "2024-01-15",
        "situacao_contrato": "ATIVO",
        "sessoes_terapia_ano": 4,
        "valor_reembolsado_ano": "150.50",
    }
    b = BeneficiarioData.model_validate(raw_v1)
    assert b.carteirinha == "1234567890123456"
    assert b.plano == "ESSENCIAL"
    assert b.sessoes_terapia_ano == 4
    assert b.acumulado_terapia == 0
    assert b.valor_reembolsado_ano == Decimal("150.50")


def test_beneficiario_v2_parsing():
    raw_v2 = {
        "carteirinha": "1234567890123456",
        "nome": "Maria Santos",
        "cpf": "987.***.***-11",
        "plano": "EXECUTIVO",
        "data_adesao": "2023-05-10",
        "situacao_contrato": "ATIVO",
        "sessoes_terapia": {
            "ano_corrente": 8,
            "acumulado": 24,
        },
        "valor_reembolsado_ano": 800.00,
    }
    b = BeneficiarioData.model_validate(raw_v2)
    assert b.carteirinha == "1234567890123456"
    assert b.plano == "EXECUTIVO"
    assert b.sessoes_terapia_ano == 8
    assert b.acumulado_terapia == 24
    assert b.valor_reembolsado_ano == Decimal("800.0")


def test_beneficiario_normaliza_status_do_servidor():
    b = BeneficiarioData.model_validate({
        "carteirinha": "1234567890123456",
        "status": "SUSPENSO",
    })
    assert b.situacao_contrato == "SUSPENSO"


def test_historico_parsing():
    raw = {
        "carteirinha": "1234567890123456",
        "pedidos": [
            {
                "protocolo": "20260000001",
                "data_solicitacao": "2026-03-01",
                "categoria": "SESSAO_TERAPIA",
                "procedimento": "50000462",
                "valor_solicitado": "250.00",
                "valor_reembolsado": "123.63",
                "status": "PAGO",
                "sessao": 5,
            }
        ],
    }
    h = HistoricoData.model_validate(raw)
    assert len(h.pedidos) == 1
    assert h.pedidos[0].numero_sessao == 5
    assert h.pedidos[0].valor_reembolsado_brl == Decimal("123.63")


def test_historico_aceita_decisao_como_status():
    item = HistoricoItem.model_validate({
        "categoria": "SESSAO_TERAPIA",
        "decisao": "APROVADO_PARCIAL",
        "valor_reembolsado_brl": "100.00",
    })
    assert item.status == "APROVADO_PARCIAL"
