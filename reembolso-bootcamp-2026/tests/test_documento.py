"""Testes unitários para o extrator documental e OCR."""

from pathlib import Path
from app.agents.documento.extractor import extract_document_info
from app.schemas import Categoria

ANEXOS_DIR = Path("anexos/treino").resolve()


def test_extracao_conta_energia_invalido():
    path = ANEXOS_DIR / "conta_energia.pdf"
    if path.exists():
        data = extract_document_info(path.read_bytes(), path.name, "application/pdf")
        assert data.categoria == Categoria.INVALIDO
        assert not data.eh_documento_fiscal_assistencial


def test_extracao_consulta_dermatologia():
    path = ANEXOS_DIR / "recibo_consulta_dermatologia.pdf"
    if path.exists():
        data = extract_document_info(path.read_bytes(), path.name, "application/pdf")
        assert data.categoria == Categoria.CONSULTA_MEDICA
        assert data.valor_pago_brl == 240.00
        assert data.codigo_procedimento == "10101012"
        assert data.data_atendimento == "2026-04-30"


def test_extracao_psicoterapia():
    path = ANEXOS_DIR / "recibo_psicoterapia.pdf"
    if path.exists():
        data = extract_document_info(path.read_bytes(), path.name, "application/pdf")
        assert data.categoria == Categoria.SESSAO_TERAPIA
        assert data.valor_pago_brl == 320.00
        assert data.codigo_procedimento == "50000462"
        assert "numero_sessao" in data.campos_ausentes


def test_extracao_relatorio_clinico():
    path = ANEXOS_DIR / "relatorio_clinico_psicoterapia.pdf"
    if path.exists():
        data = extract_document_info(path.read_bytes(), path.name, "application/pdf")
        assert data.categoria == Categoria.RELATORIO_CLINICO


def test_extracao_protese_opme():
    path = ANEXOS_DIR / "nota_protese_ortopedica.pdf"
    if path.exists():
        data = extract_document_info(path.read_bytes(), path.name, "application/pdf")
        assert data.categoria == Categoria.MATERIAL_OPME
        assert data.valor_pago_brl == 9200.00
