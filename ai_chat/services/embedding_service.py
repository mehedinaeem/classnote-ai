"""Local-only multilingual embeddings, initialized lazily once per process."""
import logging
import os
from pathlib import Path
import threading

from django.conf import settings

logger = logging.getLogger(__name__)
_model = None
_model_key = None
_model_pid = None
_lock = threading.RLock()


class EmbeddingError(Exception):
    pass


def _reset():
    global _model, _model_key, _model_pid, _lock
    _model = _model_key = _model_pid = None
    _lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset)


def model_identity():
    return {
        "model": settings.EMBEDDING_MODEL,
        "path": str(Path(settings.EMBEDDING_MODEL_PATH).resolve()),
        "revision": settings.EMBEDDING_REVISION,
        "device": settings.EMBEDDING_DEVICE,
        "query_prefix": settings.EMBEDDING_QUERY_PREFIX,
        "passage_prefix": settings.EMBEDDING_PASSAGE_PREFIX,
    }


def _get_model():
    global _model, _model_key, _model_pid
    key = model_identity()
    with _lock:
        if _model is not None and _model_pid == os.getpid():
            if _model_key != key:
                raise EmbeddingError("Embedding configuration changed. Restart Django.")
            return _model
        path = Path(settings.EMBEDDING_MODEL_PATH)
        if not (path / "modules.json").is_file():
            raise EmbeddingError("Local embedding model is missing. Run python manage.py download_embedding_model.")
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(
                str(path), device=settings.EMBEDDING_DEVICE,
                local_files_only=True, trust_remote_code=False,
            )
        except Exception as exc:
            logger.exception("Unable to load local sentence-transformer.")
            raise EmbeddingError("Could not load local embeddings. Install requirements-chat.txt and download_embedding_model.") from exc
        _model, _model_key, _model_pid = model, key, os.getpid()
        logger.info("Loaded local embedding model in process %s.", _model_pid)
        return model


def passage_fits(text):
    with _lock:
        model = _get_model()
        limit = min(settings.RAG_CHUNK_TOKENS, model.max_seq_length)
        tokens = model.tokenizer(
            settings.EMBEDDING_PASSAGE_PREFIX + text, add_special_tokens=True, truncation=False,
        )["input_ids"]
        return len(tokens) <= limit


def _encode(texts, prefix):
    with _lock:
        model = _get_model()
        try:
            for text in texts:
                if len(model.tokenizer(prefix + text, add_special_tokens=True, truncation=False)["input_ids"]) > model.max_seq_length:
                    raise EmbeddingError("Text exceeds the local embedding model limit. Shorten the question.")
            return model.encode(
                [prefix + text for text in texts],
                normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False,
                batch_size=16,
            )
        except EmbeddingError:
            raise
        except Exception as exc:
            logger.exception("Local embedding inference failed.")
            raise EmbeddingError("Local embeddings failed. Check the model and available memory.") from exc


def embed_documents(texts):
    return _encode(texts, settings.EMBEDDING_PASSAGE_PREFIX)


def embed_question(question):
    return _encode([question], settings.EMBEDDING_QUERY_PREFIX)
