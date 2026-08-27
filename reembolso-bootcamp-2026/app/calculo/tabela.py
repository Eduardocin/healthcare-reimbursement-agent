"""Catálogo determinístico de procedimentos persistido junto ao índice."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from app.schemas import Categoria

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "storage" / "procedures.json"


@dataclass(frozen=True)
class ProcedureRule:
    code: str
    description: str
    category: Categoria
    ceiling_urs: Decimal | None
    ceiling_brl: Decimal | None
    requires_human_review: bool
    requires_medical_order: bool
    requires_clinical_indication: bool
    rule_ids: tuple[str, ...]


@lru_cache(maxsize=1)
def load_procedure_catalog() -> dict[str, ProcedureRule]:
    if not CATALOG_PATH.is_file():
        raise FileNotFoundError(
            f"catálogo de procedimentos ausente em {CATALOG_PATH}; "
            "execute python -m ingest.catalog"
        )
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog: dict[str, ProcedureRule] = {}
    for code, item in raw.items():
        catalog[code] = ProcedureRule(
            code=code,
            description=str(item["description"]),
            category=Categoria(item["category"]),
            ceiling_urs=(
                Decimal(str(item["ceiling_urs"]))
                if item.get("ceiling_urs") is not None
                else None
            ),
            ceiling_brl=(
                Decimal(str(item["ceiling_brl"]))
                if item.get("ceiling_brl") is not None
                else None
            ),
            requires_human_review=bool(item.get("requires_human_review")),
            requires_medical_order=bool(item.get("requires_medical_order")),
            requires_clinical_indication=bool(item.get("requires_clinical_indication")),
            rule_ids=tuple(item.get("rule_ids", ())),
        )
    return catalog


def get_procedure_rule(code: str | None) -> ProcedureRule | None:
    if not code:
        return None
    return load_procedure_catalog().get(code.strip())
