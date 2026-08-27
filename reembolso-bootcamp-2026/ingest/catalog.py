"""Gera o catálogo determinístico de procedimentos usado pelo motor de cálculo."""

from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal
from pathlib import Path

from ingest.extract import build_nodes

ROOT = Path(__file__).resolve().parents[1]
KB_DIR = ROOT / "kb"
OUTPUT_PATH = ROOT / "storage" / "procedures.json"

CEILING_PATTERN = re.compile(
    r"(?P<urs>\d+(?:,\d+)?)\s+R\$\s*(?P<brl>[\d.]+,\d{2})",
    re.IGNORECASE,
)


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _category(text: str) -> str:
    normalized = _normalize(text)
    if "material / opme" in normalized:
        return "MATERIAL_OPME"
    if "sessao de terapia" in normalized:
        return "SESSAO_TERAPIA"
    if "exame diagnostico" in normalized:
        return "EXAME_DIAGNOSTICO"
    if "relatorio clinico" in normalized:
        return "RELATORIO_CLINICO"
    return "CONSULTA_MEDICA"


def build_catalog() -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    for node in build_nodes(KB_DIR):
        code = str(node.metadata.get("procedure_code") or "")
        if not code:
            continue
        text = " ".join(node.get_content().split())
        normalized = _normalize(text)
        ceiling = CEILING_PATTERN.search(text)
        lines = [line.strip() for line in node.get_content().splitlines() if line.strip()]
        description = lines[1] if len(lines) > 1 else code
        rule_ids = [
            value
            for value in str(node.metadata.get("rule_ids", "")).split(",")
            if value
        ]
        catalog[code] = {
            "description": description,
            "category": _category(text),
            "ceiling_urs": (
                str(Decimal(ceiling.group("urs").replace(",", ".")))
                if ceiling
                else None
            ),
            "ceiling_brl": (
                str(Decimal(ceiling.group("brl").replace(".", "").replace(",", ".")))
                if ceiling
                else None
            ),
            "requires_human_review": "sob analise" in normalized,
            "requires_medical_order": (
                "alta complexidade" in normalized or "pedido medico" in normalized
            ),
            "requires_clinical_indication": "indicacao clinica" in normalized,
            "rule_ids": rule_ids,
        }
    if not catalog:
        raise ValueError("nenhum procedimento foi extraído da Tabela URS")
    return dict(sorted(catalog.items()))


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    catalog = build_catalog()
    OUTPUT_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"catálogo construído: {len(catalog)} procedimentos em {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
