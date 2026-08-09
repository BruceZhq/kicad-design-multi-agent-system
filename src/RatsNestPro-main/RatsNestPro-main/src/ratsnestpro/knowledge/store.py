"""Retrieval knowledge base (soft knowledge only).

Two-tier knowledge stance: *hard facts* (catalog, gate thresholds, load-cap
formulas) live in the verification layer and are authoritative. This module
holds *soft knowledge* — design patterns, best practices, and the finding
taxonomy — that advises the LLM roles. Retrieved text is never treated as fact;
it only enriches prompts and its influence is still checked downstream.

Retrieval degrades gracefully:
- With an EricAI embedder, documents and the query are embedded (bge-m3) and
  ranked by cosine similarity, optionally reranked (bge-reranker-v2-m3).
- Offline (no embedder), a dependency-free lexical score (token overlap with
  inverse document frequency) is used so the agent still runs.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

_CORPUS_DIR = Path(__file__).parent / "corpus"
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass
class Doc:
    id: str
    text: str
    role: str = "general"
    source: str = ""

    def roles(self) -> list[str]:
        return [r.strip() for r in self.role.split(",") if r.strip()]


@dataclass
class Retrieved:
    doc: Doc
    score: float


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, docs: list[str], top_n: int) -> list[int]: ...


class EricAIEmbedder:
    """Lazy EricAI embedding via bge-m3."""

    def __init__(self, model: str = "BAAI/bge-m3/interactive") -> None:
        self.model = model
        self._client = None

    def _ensure(self) -> None:
        if self._client is None:
            from ericai import EricAI  # type: ignore[import-not-found]

            self._client = EricAI()

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - network
        self._ensure()
        assert self._client is not None
        return self._client.ericai.embed_texts(self.model, texts)


class EricAIReranker:
    """Lazy EricAI reranker via bge-reranker-v2-m3."""

    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3") -> None:
        self.model = model
        self._client = None

    def _ensure(self) -> None:
        if self._client is None:
            from ericai import EricAI  # type: ignore[import-not-found]

            self._client = EricAI()

    def rerank(self, query: str, docs: list[str], top_n: int) -> list[int]:  # pragma: no cover
        self._ensure()
        assert self._client is not None
        res = self._client.ericai.rerank(self.model, query, docs, top_n=top_n)
        return [r.index for r in res.results]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class KnowledgeBase:
    def __init__(self, embedder: Embedder | None = None, reranker: Reranker | None = None) -> None:
        self.embedder = embedder
        self.reranker = reranker
        self.docs: list[Doc] = []
        self._doc_tokens: list[Counter[str]] = []
        self._df: Counter[str] = Counter()
        self._embeddings: list[list[float]] | None = None

    # -- indexing ---------------------------------------------------------- #

    def add(self, docs: list[Doc]) -> None:
        for doc in docs:
            self.docs.append(doc)
            toks = Counter(_tokenize(doc.text))
            self._doc_tokens.append(toks)
            for term in toks:
                self._df[term] += 1
        self._embeddings = None  # invalidate cache

    def add_markdown_dir(self, path: str | Path, default_role: str = "general") -> int:
        directory = Path(path)
        count = 0
        for md in sorted(directory.glob("*.md")):
            text = md.read_text(encoding="utf-8")
            role = default_role
            m = re.search(r"^role:\s*(.+)$", text, re.MULTILINE)
            if m:
                role = m.group(1).strip()
            self.add([Doc(id=md.stem, text=text, role=role, source=str(md))])
            count += 1
        return count

    # -- retrieval --------------------------------------------------------- #

    def _candidates(self, role: str | None) -> list[int]:
        if role is None:
            return list(range(len(self.docs)))
        return [i for i, d in enumerate(self.docs) if role in d.roles() or "general" in d.roles()]

    def _lexical_score(self, query: str, idx: int) -> float:
        q = Counter(_tokenize(query))
        toks = self._doc_tokens[idx]
        n = len(self.docs)
        score = 0.0
        for term, qf in q.items():
            if term in toks:
                idf = math.log(1 + n / (1 + self._df[term]))
                score += qf * toks[term] * idf
        return score

    def _ensure_embeddings(self) -> None:  # pragma: no cover - network
        if self.embedder is None or self._embeddings is not None:
            return
        self._embeddings = self.embedder.embed([d.text for d in self.docs])

    def retrieve(
        self, query: str, top_k: int = 5, role: str | None = None
    ) -> list[Retrieved]:
        cand = self._candidates(role)
        if not cand:
            return []

        if self.embedder is not None:  # pragma: no cover - network
            self._ensure_embeddings()
            assert self._embeddings is not None
            qvec = self.embedder.embed([query])[0]
            scored = [(i, _cosine(qvec, self._embeddings[i])) for i in cand]
        else:
            scored = [(i, self._lexical_score(query, i)) for i in cand]

        scored.sort(key=lambda t: t[1], reverse=True)
        top = scored[: max(top_k, 1)]

        if self.reranker is not None and len(top) > 1:  # pragma: no cover - network
            order = self.reranker.rerank(query, [self.docs[i].text for i, _ in top], top_k)
            top = [top[j] for j in order if 0 <= j < len(top)]

        return [Retrieved(doc=self.docs[i], score=float(s)) for i, s in top[:top_k]]

    def retrieve_text(self, query: str, top_k: int = 3, role: str | None = None) -> str:
        """Convenience: concatenated snippets for injecting into a prompt."""
        hits = self.retrieve(query, top_k=top_k, role=role)
        return "\n\n".join(f"[{h.doc.id}]\n{h.doc.text.strip()}" for h in hits)


def build_default_kb(
    embedder: Embedder | None = None, reranker: Reranker | None = None
) -> KnowledgeBase:
    """A KnowledgeBase pre-loaded with the bundled soft-knowledge corpus."""
    kb = KnowledgeBase(embedder=embedder, reranker=reranker)
    kb.add_markdown_dir(_CORPUS_DIR)
    return kb
