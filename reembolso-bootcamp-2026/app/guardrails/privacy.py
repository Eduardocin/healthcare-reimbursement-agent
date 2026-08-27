"""Guardrails de privacidade e integridade:
- Mascaramento de CPF em qualquer texto gerado.
- Detecção e remoção/bloqueio de códigos CID ou hipóteses diagnósticas.
- Detecção e tratamento de consultas sobre terceiros (carteirinha diferente).
"""

from __future__ import annotations

import re

# Padrão para CPFs formatados (ex: 123.456.789-00)
CPF_FORMATTED_PATTERN = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
# Padrão para CPFs em 11 dígitos contínuos (evitando números de protocolo como 2026xxxxxxx)
CPF_DIGITS_PATTERN = re.compile(r"\b(?!2026\d{7}\b)\d{11}\b")

# Padrão para códigos CID-10 (Ex: F32, F32.1, M54, M54.5, Z00, etc.)
CID_PATTERN = re.compile(
    r"\b(?:CID[-\s]?10|CID)?\s*([A-Z]\d{2}(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)

# Padrão para carteirinhas da operadora (16 dígitos com ou sem espaços/pontos/hífens)
CARTEIRINHA_PATTERN = re.compile(r"(?<!\d)(?:\d[\s.-]*){16}(?!\d)")


def extract_carteirinhas(text: str) -> list[str]:
    """Extrai todos os números de carteirinha com 16 dígitos encontrados no texto."""
    if not text:
        return []
    matches = CARTEIRINHA_PATTERN.findall(text)
    results = []
    for m in matches:
        digits = re.sub(r"\D", "", m)
        if len(digits) == 16 and digits not in results:
            results.append(digits)
    return results


def mask_cpf(text: str) -> str:
    """Mascara CPFs presentes no texto, garantindo que o número completo nunca seja exibido."""
    if not text:
        return text

    def _replace_formatted(match: re.Match) -> str:
        s = match.group(0)
        parts = s.split(".")
        last_part = parts[2].split("-")
        return f"{parts[0]}.***.***-{last_part[1]}"

    text = CPF_FORMATTED_PATTERN.sub(_replace_formatted, text)

    # Mascara também sequências de 11 dígitos contínuos que não sejam protocolos 2026xxxxxxx
    def _replace_digits(match: re.Match) -> str:
        s = match.group(0)
        return f"{s[:3]}.***.***-{s[-2:]}"

    text = CPF_DIGITS_PATTERN.sub(_replace_digits, text)
    return text


def sanitize_text(text: str, bound_carteirinha: str | None = None) -> str:
    """Aplica mascaramento de CPF, formata protocolos, remove CIDs e remove carteirinhas de terceiros."""
    if not text:
        return ""

    # 1. Neutraliza ou mascara qualquer carteirinha de 16 dígitos que não seja a do titular
    found_cards = extract_carteirinhas(text)
    for c in found_cards:
        if bound_carteirinha is None or c != bound_carteirinha:
            # Substitui todas as representações possíveis desse número
            # Gera regex que aceita espaços/pontos entre dígitos
            digits = [re.escape(d) for d in c]
            spaced_pattern = r"(?<!\d)" + r"[\s.-]*".join(digits) + r"(?!\d)"
            text = re.sub(spaced_pattern, "[carteirinha de terceiro]", text)

    # 2. Formata números de protocolo (ex: 20260000001 -> 2026-0000001) no texto para evitar colisão com regex de CPF
    text = re.sub(r"\b(2026)(\d{7})\b", r"\1-\2", text)

    # 3. Mascara CPF
    sanitized = mask_cpf(text)

    # 4. Remove ou neutraliza códigos CID explícitos para atender ao art. 7º
    sanitized = re.sub(
        r"\b(?:CID[-\s]?10|CID)\s*:?\s*[A-Z]\d{2}(?:\.\d{1,2})?\b",
        "[diagnóstico protegido]",
        sanitized,
        flags=re.IGNORECASE,
    )
    # Remove qualquer menção avulsa a padrão de CID
    sanitized = re.sub(
        r"\b[A-TV-Z]\d{2}(?:\.\d)?\b",
        "[código protegido]",
        sanitized,
    )

    return sanitized


def check_third_party_request(
    message: str,
    bound_carteirinha: str | None,
) -> tuple[bool, str | None]:
    """Verifica se a mensagem contém uma tentativa de consulta sobre terceiro."""
    if not bound_carteirinha:
        return False, None

    found = extract_carteirinhas(message)
    for c in found:
        if c != bound_carteirinha:
            return True, c

    # Também detecta termos indicando consulta a dependente/cônjuge
    msg_l = message.lower()
    if any(term in msg_l for term in ["esposa", "marido", "filho", "filha", "cônjuge", "dependente", "outra pessoa"]):
        return True, found[0] if found else None

    return False, None
