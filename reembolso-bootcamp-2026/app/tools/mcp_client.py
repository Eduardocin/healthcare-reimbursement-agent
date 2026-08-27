"""Cliente assíncrono para o servidor MCP da operadora.

Suporta transporte Streamable HTTP, token de autenticação via header Authorization,
e adapters Pydantic tolerantes aos esquemas v1 e v2.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field, model_validator


class BeneficiarioData(BaseModel):
    carteirinha: str
    nome: str | None = None
    cpf: str | None = None
    plano: str | None = None
    data_adesao: str | None = None
    situacao_contrato: str | None = None
    sessoes_terapia_ano: int = 0
    acumulado_terapia: int = 0
    valor_reembolsado_ano: Decimal = Decimal("0.00")
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_schemes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        # Schema v1 vs v2 normalization
        sessoes_terapia_ano = data.get("sessoes_terapia_ano")
        acumulado_terapia = data.get("acumulado_terapia", 0)

        sessoes_obj = data.get("sessoes_terapia")
        if isinstance(sessoes_obj, dict):
            if sessoes_terapia_ano is None:
                sessoes_terapia_ano = sessoes_obj.get("ano_corrente", 0)
            if not acumulado_terapia:
                acumulado_terapia = sessoes_obj.get("acumulado", 0)

        if sessoes_terapia_ano is None:
            sessoes_terapia_ano = 0

        # Normaliza valor_reembolsado_ano
        val_reemb = data.get("valor_reembolsado_ano", 0)
        if val_reemb is None:
            val_reemb = Decimal("0.00")
        elif isinstance(val_reemb, (int, float, str)):
            val_reemb = Decimal(str(val_reemb))

        return {
            **data,
            "situacao_contrato": data.get("situacao_contrato") or data.get("status"),
            "sessoes_terapia_ano": int(sessoes_terapia_ano),
            "acumulado_terapia": int(acumulado_terapia or 0),
            "valor_reembolsado_ano": val_reemb,
            "raw": data,
        }


class HistoricoItem(BaseModel):
    protocolo: str | None = None
    data_solicitacao: str | None = None
    categoria: str | None = None
    procedimento: str | None = None
    valor_solicitado_brl: Decimal | None = None
    valor_reembolsado_brl: Decimal | None = None
    status: str | None = None
    numero_sessao: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_item(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        # Mapeamento flexível de chaves
        val_sol = (
            data.get("valor_solicitado_brl")
            or data.get("valor_solicitado")
            or data.get("valor")
        )
        val_reemb = (
            data.get("valor_reembolsado_brl")
            or data.get("valor_reembolso_brl")
            or data.get("valor_reembolsado")
        )
        data_sol = (
            data.get("data_solicitacao")
            or data.get("data")
            or data.get("data_atendimento")
        )
        num_sess = (
            data.get("numero_sessao")
            or data.get("sessao")
            or data.get("sessao_numero")
        )

        return {
            "protocolo": data.get("protocolo"),
            "data_solicitacao": str(data_sol) if data_sol else None,
            "categoria": data.get("categoria"),
            "procedimento": data.get("procedimento") or data.get("codigo_procedimento"),
            "valor_solicitado_brl": Decimal(str(val_sol)) if val_sol is not None else None,
            "valor_reembolsado_brl": Decimal(str(val_reemb)) if val_reemb is not None else None,
            "status": data.get("status") or data.get("decisao"),
            "numero_sessao": int(num_sess) if num_sess is not None else None,
            "raw": data,
        }


class HistoricoData(BaseModel):
    carteirinha: str
    pedidos: list[HistoricoItem] = Field(default_factory=list)


class ProtocoloData(BaseModel):
    protocolo: str
    carteirinha: str
    status: str = "EM_ANALISE"
    aberto_em: str | None = None


class MCPClientError(Exception):
    """Erro ao comunicar ou executar ferramenta no servidor MCP."""
    pass


class MCPOperadoraClient:
    """Cliente para falar com o servidor MCP da operadora."""

    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = url or os.getenv("MCP_OPERADORA_URL", "").strip()
        self.token = token if token is not None else os.getenv("MCP_OPERADORA_TOKEN", "").strip()
        if not self.url:
            raise MCPClientError("MCP_OPERADORA_URL não foi configurada")
        if not self.token:
            raise MCPClientError("MCP_OPERADORA_TOKEN não foi configurado")

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        try:
            headers = self._headers()
            async with httpx.AsyncClient(headers=headers, timeout=10.0) as http_client:
                async with streamable_http_client(self.url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments)

                        if getattr(result, "isError", False):
                            err_text = ""
                            for c in getattr(result, "content", []):
                                if hasattr(c, "text"):
                                    err_text += c.text + " "
                            raise MCPClientError(f"Erro retornado pela ferramenta {tool_name}: {err_text.strip()}")

                        for c in getattr(result, "content", []):
                            if hasattr(c, "text"):
                                try:
                                    return json.loads(c.text)
                                except json.JSONDecodeError:
                                    return c.text
                        return None
        except MCPClientError:
            raise
        except Exception as exc:
            raise MCPClientError(
                f"Falha ao executar a ferramenta MCP {tool_name}"
            ) from exc

    async def consultar_beneficiario(self, carteirinha: str) -> BeneficiarioData:
        data = await self._call_tool("consultar_beneficiario", {"carteirinha": carteirinha})
        if not isinstance(data, dict):
            raise MCPClientError(f"Resposta inesperada de consultar_beneficiario: {data}")
        return BeneficiarioData.model_validate(data)

    async def consultar_historico(self, carteirinha: str) -> HistoricoData:
        data = await self._call_tool("consultar_historico", {"carteirinha": carteirinha})
        if not isinstance(data, dict):
            raise MCPClientError(f"Resposta inesperada de consultar_historico: {data}")
        return HistoricoData.model_validate(data)

    async def abrir_protocolo(self, carteirinha: str, payload: dict[str, Any]) -> ProtocoloData:
        data = await self._call_tool("abrir_protocolo", {"carteirinha": carteirinha, "payload": payload})
        if not isinstance(data, dict):
            raise MCPClientError(f"Resposta inesperada de abrir_protocolo: {data}")
        return ProtocoloData.model_validate(data)

    def consultar_beneficiario_sync(self, carteirinha: str) -> BeneficiarioData:
        return run_sync(self.consultar_beneficiario(carteirinha))

    def consultar_historico_sync(self, carteirinha: str) -> HistoricoData:
        return run_sync(self.consultar_historico(carteirinha))

    def abrir_protocolo_sync(self, carteirinha: str, payload: dict[str, Any]) -> ProtocoloData:
        return run_sync(self.abrir_protocolo(carteirinha, payload))


def run_sync(coro):
    """Executa uma corotina de forma síncrona com segurança."""
    import asyncio
    import nest_asyncio

    nest_asyncio.apply()
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return loop.run_until_complete(coro)
