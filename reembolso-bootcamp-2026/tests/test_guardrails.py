"""Testes unitários para guardrails de privacidade e integridade."""

from app.guardrails.privacy import (
    check_third_party_request,
    extract_carteirinhas,
    mask_cpf,
    sanitize_text,
)


def test_mask_cpf():
    text = "O paciente João com CPF 528.417.930-11 realizou a consulta."
    masked = mask_cpf(text)
    assert "528.417.930-11" not in masked
    assert "528.***.***-11" in masked


def test_sanitize_text_cid():
    text = "Paciente diagnosticado com CID-10 F32.1 em acompanhamento."
    sanitized = sanitize_text(text)
    assert "F32.1" not in sanitized
    assert "[diagnóstico protegido]" in sanitized


def test_extract_carteirinhas():
    text = "Minha carteirinha é 7042.8813.5561.0029 e a da esposa é 7042519908873310."
    found = extract_carteirinhas(text)
    assert "7042881355610029" in found
    assert "7042519908873310" in found


def test_check_third_party_request():
    bound = "7042881355610029"
    msg_same = "Quero saber do meu reembolso na carteirinha 7042881355610029"
    is_third, third_c = check_third_party_request(msg_same, bound)
    assert not is_third

    msg_third = "E o plano da minha esposa na carteirinha 7042519908873310?"
    is_third, third_c = check_third_party_request(msg_third, bound)
    assert is_third
    assert third_c == "7042519908873310"

    is_third, third_c = check_third_party_request(
        "Você pode consultar o reembolso da minha esposa?",
        bound,
    )
    assert is_third
    assert third_c is None
