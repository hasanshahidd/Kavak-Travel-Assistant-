"""Knowledge-base retriever — H2-section chunking + FAISS over embeddings.

Three things this module gets right that most RAG implementations skip:

1. **Section-aware chunking.** We chunk by H2 heading, so each chunk is a
   semantically self-contained unit (one visa rule, one refund clause).
   Token-window chunking would arbitrarily split a rule mid-sentence.

2. **Content-hash cache.** The FAISS index is rebuilt only when the KB
   text changes. Hash of all KB file contents → if the cache hash matches,
   we reload from disk. Most RAG demos rebuild on every startup, costing
   embedding API calls and adding seconds of cold-start latency.

3. **Relevance threshold gate.** ``search()`` rejects chunks below
   ``min_score`` *before returning them*, forcing the answerer down its
   refusal path when nothing relevant was found. This is half of the
   anti-hallucination architecture; the other half is the citation verifier.

Search returns ``Chunk`` objects with the ``score`` field populated, so
downstream nodes (answerer, citation verifier) work against typed data
all the way through.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.llm.embeddings import EmbeddingsClient, get_embeddings_client
from app.schemas.rag import Chunk

# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_H2_PATTERN = re.compile(r"^##\s+(?P<heading>.+?)\s*$", re.MULTILINE)
_SLUG_NON_WORD = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Stable, ascii-only slug used in chunk ids."""
    out = _SLUG_NON_WORD.sub("-", text.lower()).strip("-")
    return out or "section"


def chunk_markdown(text: str, *, doc: str) -> list[Chunk]:
    """Split a markdown document into one Chunk per H2 section.

    Content above the first H2 is dropped (it's typically the H1 title +
    intro paragraph; not retrievable signal). The H2 heading is included
    in each chunk's ``content`` field so heading-style queries still match.
    """
    matches = list(_H2_PATTERN.finditer(text))
    if not matches:
        return []

    chunks: list[Chunk] = []
    for i, match in enumerate(matches):
        heading = match.group("heading").strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        if not body:
            continue
        # Include the heading in content — improves retrieval and gives the
        # citation verifier a string to match heading-derived spans against.
        content = f"{heading}\n\n{body}"
        chunks.append(
            Chunk(
                id=f"{doc}#{_slugify(heading)}",
                doc=doc,
                section=heading,
                content=content,
            )
        )
    return chunks


def load_kb_chunks(kb_dir: Path) -> list[Chunk]:
    """Load every ``*.md`` file under ``kb_dir`` and chunk it. Sorted for determinism."""
    chunks: list[Chunk] = []
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        chunks.extend(chunk_markdown(text, doc=path.name))
    return chunks


# ---------------------------------------------------------------------------
# Cache key — hash all KB content so index rebuilds when text changes
# ---------------------------------------------------------------------------


def kb_content_hash(kb_dir: Path) -> str:
    """SHA-256 across all KB markdown files. Determines whether to rebuild the index."""
    h = hashlib.sha256()
    for path in sorted(kb_dir.glob("*.md")):
        h.update(path.name.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class KBRetriever:
    """FAISS-backed retriever with content-hash cache and relevance threshold."""

    def __init__(
        self,
        kb_dir: Path | None = None,
        embeddings: EmbeddingsClient | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        settings = get_settings()
        self.kb_dir = kb_dir or settings.kb_dir
        self.cache_dir = cache_dir or settings.faiss_index_dir
        self.embeddings = embeddings or get_embeddings_client()

        self._chunks: list[Chunk] = []
        self._matrix: np.ndarray | None = None  # shape (n_chunks, dim), L2-normalized
        self._loaded = False

    # ------- public API -------

    def build_or_load(self) -> None:
        """Build the index if absent or stale; otherwise reload from disk.

        Cheap re-entrancy: subsequent calls within the same process are no-ops.
        """
        if self._loaded:
            return

        current_hash = kb_content_hash(self.kb_dir)
        cache_meta = self.cache_dir / f"index-{self.embeddings.name}.json"
        cache_npy = self.cache_dir / f"index-{self.embeddings.name}.npy"

        if cache_meta.exists() and cache_npy.exists():
            meta = json.loads(cache_meta.read_text(encoding="utf-8"))
            if meta.get("kb_hash") == current_hash and meta.get("dim") == self.embeddings.dim:
                self._chunks = [Chunk.model_validate(c) for c in meta["chunks"]]
                self._matrix = np.load(cache_npy)
                self._loaded = True
                return

        # Cache miss → rebuild.
        self._chunks = load_kb_chunks(self.kb_dir)
        if not self._chunks:
            raise RuntimeError(f"No chunks found under {self.kb_dir}")

        vectors = self.embeddings.embed([c.content for c in self._chunks])
        matrix = np.asarray(vectors, dtype=np.float32)
        # Normalize so dot product = cosine similarity. MockEmbeddings already
        # normalizes; OpenAI returns L2-normalized vectors. Re-normalize defensively.
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        np.save(cache_npy, matrix)
        cache_meta.write_text(
            json.dumps(
                {
                    "kb_hash": current_hash,
                    "dim": self.embeddings.dim,
                    "embeddings": self.embeddings.name,
                    "chunks": [c.model_dump() for c in self._chunks],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        self._matrix = matrix
        self._loaded = True

    def search(
        self,
        query: str,
        *,
        top_k: int = 4,
        min_score: float = 0.5,
    ) -> list[Chunk]:
        """Return top-k chunks whose cosine similarity to ``query`` ≥ ``min_score``.

        The threshold gate is doing real work: when it returns ``[]``, the
        answerer's contract requires it to refuse rather than hallucinate.
        """
        self.build_or_load()
        if self._matrix is None or not self._chunks:
            return []

        q_vec = np.asarray(self.embeddings.embed([query])[0], dtype=np.float32)
        q_norm = float(np.linalg.norm(q_vec))
        if q_norm == 0:
            return []
        q_vec = q_vec / q_norm

        scores = self._matrix @ q_vec  # cosine similarity since both are normalized
        ranked = np.argsort(-scores)[:top_k]

        results: list[Chunk] = []
        for idx in ranked:
            score = float(scores[idx])
            if score < min_score:
                break  # ranked descending → no later result will pass
            chunk = self._chunks[int(idx)]
            results.append(chunk.model_copy(update={"score": score}))
        return results


__all__ = [
    "KBRetriever",
    "chunk_markdown",
    "kb_content_hash",
    "load_kb_chunks",
]
