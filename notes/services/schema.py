"""Strict academic-note schema and grounding checks; no model imports."""
import json
import math
import re

from django.core.exceptions import ValidationError

KEYS = ("title", "summary", "topics", "key_points", "definitions", "examples",
        "formulas", "exam_important", "homework", "questions_asked")


def empty_note_data():
    return {key: "" if key in ("title", "summary") else [] for key in KEYS}


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
TIME = {"type": ["number", "null"], "minimum": 0}
NOTE_SCHEMA = _object({
    "title": {"type": "string"}, "summary": {"type": "string"},
    "topics": {"type": "array", "items": _object({"title": TEXT, "explanation": TEXT, "start_time": TIME})},
    "key_points": {"type": "array", "items": TEXT},
    "definitions": {"type": "array", "items": _object({"term": TEXT, "definition": TEXT})},
    "examples": {"type": "array", "items": TEXT},
    "formulas": {"type": "array", "items": TEXT},
    "exam_important": {"type": "array", "items": _object({"text": TEXT, "reason": TEXT, "start_time": TIME})},
    "homework": {"type": "array", "items": TEXT},
    "questions_asked": {"type": "array", "items": TEXT},
})


def validate_note_data(data):
    """Django JSONField validator; reject missing/extra fields and wrong types."""
    def check(value, schema, path):
        kind = schema["type"]
        if isinstance(kind, list):
            if value is None:
                return
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValidationError(f"{path} must be a finite nonnegative time or null.")
        elif kind == "object":
            if not isinstance(value, dict) or set(value) != set(schema["properties"]):
                raise ValidationError(f"{path} must contain exactly the required fields.")
            for key, child in schema["properties"].items():
                check(value[key], child, f"{path}.{key}")
        elif kind == "array":
            if not isinstance(value, list):
                raise ValidationError(f"{path} must be an array.")
            if len(value) > 100:
                raise ValidationError(f"{path} may contain at most 100 items.")
            for index, item in enumerate(value):
                check(item, schema["items"], f"{path}[{index}]")
        else:
            if not isinstance(value, str):
                raise ValidationError(f"{path} must be text.")
            if schema.get("minLength") and not value.strip():
                raise ValidationError(f"{path} cannot be an empty array entry.")
    check(data, NOTE_SCHEMA, "notes")


CUES = re.compile(
    r"\b(?:important|very important|exam|final|midterm|remember this|write this)\b"
    r"|পরীক্ষায়|পরীক্ষায়|গুরুত্বপূর্ণ|মনে রাখবে|লিখে রাখো", re.IGNORECASE,
)


def validate_grounding(data, segments):
    """Enforce timestamp provenance and verbatim exam-evidence quotes."""
    starts = {float(item["start_time"]) for item in segments}
    for item in [*data["topics"], *data["exam_important"]]:
        if item["start_time"] is not None and float(item["start_time"]) not in starts:
            raise ValidationError("Note timestamp must match a supplied transcript segment start.")
    for item in data["exam_important"]:
        quote = item["reason"].strip()
        evidence = [segment for segment in segments
                    if quote and quote.casefold() in segment["text"].casefold()]
        if not CUES.search(quote) or not evidence:
            raise ValidationError("Exam-important reason must quote actual transcript evidence with an emphasis/exam cue.")
        if item["start_time"] is not None and not any(
            float(segment["start_time"]) == float(item["start_time"]) for segment in evidence
        ):
            raise ValidationError("Exam-important timestamp must refer to its evidence segment.")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields are not allowed.")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON value")


def parse_notes(raw, *, segments, max_chars):
    if not isinstance(raw, str) or len(raw) > max_chars:
        raise ValidationError("Note response exceeds the configured output character budget.")
    try:
        data = json.loads(
            raw, object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("Return valid JSON only, with no fences, comments or duplicate fields.") from exc
    validate_note_data(data)
    validate_grounding(data, segments)
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(compact) > max_chars:
        raise ValidationError("Normalized note response exceeds the output character budget.")
    return data
