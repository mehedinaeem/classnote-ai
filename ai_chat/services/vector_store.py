"""Private, fingerprinted FAISS indexes for individual lectures."""
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading

from django.conf import settings
from django.core.exceptions import PermissionDenied

from notes.services.chunking import serialize
from .embedding_service import EmbeddingError, embed_documents, embed_question, model_identity, passage_fits

logger = logging.getLogger(__name__)
_index_lock = threading.RLock()


def _reset_lock():
    global _index_lock
    _index_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_lock)


class VectorStoreError(Exception):
    pass


def _dependencies():
    try:
        import faiss
        import numpy as np
        return faiss, np
    except (ImportError, OSError) as exc:
        raise VectorStoreError("Install requirements-chat.txt for CPU FAISS retrieval.") from exc


def transcript_snapshot(lecture, segments=None):
    return [
        {"segment_id": item.pk, "start_time": float(item.start_time),
         "end_time": float(item.end_time), "text": item.text}
        for item in (lecture.transcript_segments.all() if segments is None else segments)
    ]


def source_fingerprint(source):
    return hashlib.sha256(serialize(source).encode("utf-8")).hexdigest()


def chunk_segments(source, *, fits=None):
    fits = fits or passage_fits
    chunks = []
    for segment in source:
        text = segment["text"].strip()
        while text:
            low, high, take = 1, min(len(text), settings.RAG_CHUNK_CHARS), 0
            while low <= high:
                middle = (low + high) // 2
                if fits(text[:middle]):
                    take, low = middle, middle + 1
                else:
                    high = middle - 1
            if not take:
                raise VectorStoreError("The embedding chunk token budget is too small.")
            if take < len(text):
                boundary = text.rfind(" ", 0, take)
                if boundary > take // 2:
                    take = boundary
            chunks.append({**segment, "text": text[:take], "chunk_id": len(chunks)})
            text = text[take:].lstrip()
    return chunks


def _matrix(values, np):
    array = np.ascontiguousarray(values, dtype="float32")
    if array.ndim != 2 or not array.shape[1] or not np.isfinite(array).all():
        raise VectorStoreError("Embeddings are not a valid finite vector matrix.")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if (norms <= 0).any():
        raise VectorStoreError("An embedding has zero length.")
    return np.ascontiguousarray(array / norms, dtype="float32")


def _load(directory, key, source, faiss, np):
    metadata = json.loads((directory / "chunks.json").read_text())
    payload = (directory / "index.faiss").read_bytes()
    if metadata["key"] != key or metadata["sha256"] != hashlib.sha256(payload).hexdigest():
        raise ValueError("Index fingerprint/checksum mismatch")
    originals = {item["segment_id"]: item for item in source}
    chunks = metadata["chunks"]
    for index, chunk in enumerate(chunks):
        original = originals[chunk["segment_id"]]
        if (
            chunk["chunk_id"] != index or not chunk["text"]
            or chunk["text"] not in original["text"]
            or chunk["start_time"] != original["start_time"]
            or chunk["end_time"] != original["end_time"]
        ):
            raise ValueError("Index source metadata does not match the lecture")
    index = faiss.deserialize_index(np.frombuffer(payload, dtype="uint8"))
    if index.ntotal != len(chunks) or not index.d:
        raise ValueError("Index row count mismatch")
    return index, chunks


def _load_or_build(lecture, source, faiss, np):
    identity = {
        "version": 1, "source": source_fingerprint(source),
        "embedding": model_identity(), "chars": settings.RAG_CHUNK_CHARS,
        "tokens": settings.RAG_CHUNK_TOKENS,
    }
    key = hashlib.sha256(serialize(identity).encode()).hexdigest()
    directory = Path(settings.MEDIA_ROOT) / "lectures" / str(lecture.owner_id) / str(lecture.pk) / "search" / key
    with _index_lock:
        if directory.exists():
            try:
                return (*_load(directory, key, source, faiss, np), key)
            except Exception:
                logger.warning("Rebuilding invalid FAISS cache for lecture %s.", lecture.pk)
                shutil.rmtree(directory)
        chunks = chunk_segments(source)
        embeddings = _matrix(embed_documents([item["text"] for item in chunks]), np)
        if embeddings.shape[0] != len(chunks):
            raise VectorStoreError("Embedding count does not match transcript chunks.")
        index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)
        directory.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=directory.parent))
        try:
            payload = faiss.serialize_index(index).tobytes()
            (temporary / "index.faiss").write_bytes(payload)
            (temporary / "chunks.json").write_text(serialize({
                "key": key, "sha256": hashlib.sha256(payload).hexdigest(), "chunks": chunks,
            }))
            try:
                temporary.rename(directory)
            except OSError:
                if not directory.exists():
                    raise
                # Another process published this immutable version first.
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        logger.info("Indexed %d chunks for lecture %s.", len(chunks), lecture.pk)
        return index, chunks, key


def search_lecture(lecture, *, user, question):
    if lecture.owner_id != user.pk:
        raise PermissionDenied("This lecture is not yours.")
    source = transcript_snapshot(lecture)
    fingerprint = source_fingerprint(source)
    if not source or not any(item["text"].strip() for item in source):
        return [], fingerprint
    if (
        not 1 <= settings.RAG_TOP_K <= 8 or not 64 <= settings.RAG_CHUNK_CHARS <= 4000
        or not 8 <= settings.RAG_CHUNK_TOKENS <= 512
        or not 512 <= settings.RAG_CONTEXT_CHARS <= 16000
        or not math.isfinite(settings.RAG_MIN_SIMILARITY) or not -1 <= settings.RAG_MIN_SIMILARITY <= 1
    ):
        raise VectorStoreError("Invalid RAG retrieval configuration.")
    faiss, np = _dependencies()
    try:
        index, chunks, _ = _load_or_build(lecture, source, faiss, np)
        query = _matrix(embed_question(question), np)
        if query.shape != (1, index.d):
            raise VectorStoreError("Question embedding dimensions do not match the index.")
        scores, ids = index.search(query, min(settings.RAG_TOP_K, index.ntotal))
        selected, size = [], 0
        for score, row in zip(scores[0], ids[0]):
            if row < 0 or not math.isfinite(float(score)) or float(score) < settings.RAG_MIN_SIMILARITY:
                continue
            chunk = chunks[int(row)]
            length = len(serialize(chunk))
            if size + length > settings.RAG_CONTEXT_CHARS:
                continue
            selected.append(chunk)
            size += length
        logger.info("Retrieved %d transcript chunks for lecture %s.", len(selected), lecture.pk)
        return selected, fingerprint
    except (VectorStoreError, EmbeddingError):
        raise
    except Exception as exc:
        logger.exception("Local FAISS retrieval failed for lecture %s.", lecture.pk)
        raise VectorStoreError("Could not build or search the local lecture index.") from exc
