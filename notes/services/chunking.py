import hashlib
import json


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def transcript_source(segments):
    return [
        {"start_time": float(item.start_time), "end_time": float(item.end_time), "text": item.text}
        for item in segments
    ]


def fingerprint(segments):
    return hashlib.sha256(serialize(segments).encode("utf-8")).hexdigest()


def chunk_transcript(segments, max_chars):
    """Pack bounded JSON arrays; oversized individual segments are split in memory."""
    if max_chars < 256:
        raise ValueError("Transcript chunk size must be at least 256 characters.")
    chunks, current = [], []
    for segment in segments:
        text = segment["text"].strip()
        while text:
            # Binary search includes JSON escaping, punctuation and timestamp overhead.
            lo, hi = 1, len(text)
            take = 0
            while lo <= hi:
                middle = (lo + hi) // 2
                candidate = {**segment, "text": text[:middle]}
                if len(serialize([*current, candidate])) <= max_chars:
                    take, lo = middle, middle + 1
                else:
                    hi = middle - 1
            if not take:
                if current:
                    chunks.append(current)
                    current = []
                    continue
                raise ValueError("Chunk size is too small for transcript metadata.")
            # Prefer whitespace boundaries when splitting, retaining timestamps.
            if take < len(text):
                boundary = text.rfind(" ", 0, take)
                if boundary > take // 2:
                    take = boundary
            current.append({**segment, "text": text[:take]})
            text = text[take:].lstrip()
            if text:
                chunks.append(current)
                current = []
    if current:
        chunks.append(current)
    return chunks
