"""Modelos de embedding usados pela recuperação densa."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import unicodedata
from typing import Any

import httpx
from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import PrivateAttr

from app.llm import DIMENSOES, MODELO_EMBEDDING, _ambiente

MODEL_NAME = "local-hash-embedding-v1"
DIMENSIONS = 1536
WORD_PATTERN = re.compile(r"\b\w{2,}\b", re.UNICODE)
STOP_WORDS = frozenset({
    "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do", "dos",
    "e", "em", "entre", "essa", "esse", "esta", "este", "na", "nas", "no",
    "nos", "o", "os", "ou", "para", "pela", "pelo", "por", "que", "se", "sem",
    "ser", "sua", "suas", "um", "uma",
})


class GatewayEmbedding(BaseEmbedding):
    """Embedding semântico pelo endpoint OpenAI-compatível da banca."""

    dimensions: int = DIMENSOES
    _endpoint: str = PrivateAttr()
    _api_key: str = PrivateAttr()
    _timeout: float = PrivateAttr()

    def __init__(
        self,
        *,
        embed_batch_size: int = 100,
        timeout: float = 300.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            model_name=MODELO_EMBEDDING,
            embed_batch_size=embed_batch_size,
            **kwargs,
        )
        self._endpoint, self._api_key = _ambiente()
        self._timeout = timeout

    @classmethod
    def class_name(cls) -> str:
        return "GatewayEmbedding"

    def _embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self._endpoint}/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": MODELO_EMBEDDING,
                    "input": texts,
                    "dimensions": self.dimensions,
                },
            )
            response.raise_for_status()
        payload = response.json()
        items = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
        embeddings = [item.get("embedding") for item in items]
        if len(embeddings) != len(texts) or any(
            not isinstance(embedding, list) or len(embedding) != self.dimensions
            for embedding in embeddings
        ):
            raise ValueError("resposta de embeddings incompatível com o contrato")
        return embeddings

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._embed_many([query])[0]

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._embed_many([text])[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._embed_many(texts)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return await asyncio.to_thread(self._get_query_embedding, query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._get_text_embedding, text)

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._get_text_embeddings, texts)


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in value if not unicodedata.combining(char))


def _features(text: str) -> list[tuple[str, float]]:
    words = [
        word for word in WORD_PATTERN.findall(_normalize(text))
        if word not in STOP_WORDS
    ]
    features: list[tuple[str, float]] = [(f"w:{word}", 2.0) for word in words]
    features.extend(
        (f"b:{left}_{right}", 1.25)
        for left, right in zip(words, words[1:])
    )
    for word in words:
        padded = f"^{word}$"
        for size in (3, 4):
            features.extend(
                (f"c:{padded[index:index + size]}", 0.30)
                for index in range(max(0, len(padded) - size + 1))
            )
    return features


class LocalHashEmbedding(BaseEmbedding):
    """Feature hashing lexical em vetor denso normalizado.

    O mesmo algoritmo roda na ingestão e na consulta. Isso mantém o índice
    reproduzível, embarcado e utilizável quando a cota externa está indisponível.
    """

    dimensions: int = DIMENSIONS

    def __init__(self, dimensions: int = DIMENSIONS, **kwargs: Any) -> None:
        super().__init__(
            model_name=MODEL_NAME,
            embed_batch_size=kwargs.pop("embed_batch_size", 256),
            **kwargs,
        )
        self.dimensions = dimensions

    @classmethod
    def class_name(cls) -> str:
        return "LocalHashEmbedding"

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature, weight in _features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._embed(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._embed(text)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._embed(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._embed(text)
