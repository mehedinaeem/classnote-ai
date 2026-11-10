"""Explainable bilingual cue matching. Scores rank cues, not probabilities."""
from dataclasses import dataclass
import re
import unicodedata


def normalize(text):
    return unicodedata.normalize("NFC", text).casefold()


# Longer overlapping phrases win, so "very important" is counted once.
CUES = [
    ("very important", 2, "emphasis"), ("important", 2, "emphasis"),
    ("remember this", 1, "emphasis"), ("remember", 1, "emphasis"),
    ("write this down", 1, "emphasis"), ("write this", 1, "emphasis"),
    ("you should know this", 1, "emphasis"),
    ("exam", 3, "exam"), ("midterm", 3, "exam"), ("final", 3, "exam"),
    ("assignment", 3, "homework"), ("homework", 3, "homework"),
    ("গুরুত্বপূর্ণ", 2, "emphasis"), ("মনে রাখবে", 1, "emphasis"),
    ("মনে রাখবেন", 1, "emphasis"), ("পরীক্ষায় আসবে", 3, "exam"),
    ("পরীক্ষায় আসতে পারে", 3, "exam"), ("পরীক্ষায়", 3, "exam"),
    ("লিখে রাখো", 1, "emphasis"), ("লিখে রাখুন", 1, "emphasis"),
    ("অ্যাসাইনমেন্ট", 3, "homework"), ("হোমওয়ার্ক", 3, "homework"),
    ("হোমওয়ার্ক", 3, "homework"),
]
_LOOKUP = {normalize(phrase): (phrase, weight, family) for phrase, weight, family in CUES}
_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(phrase).replace(r"\ ", r"\s+")
                                 for phrase in sorted(_LOOKUP, key=len, reverse=True)) + r")(?!\w)"
)


def match_cues(text):
    matches = []
    seen = set()
    for match in _PATTERN.finditer(normalize(text)):
        key = re.sub(r"\s+", " ", match.group())
        if key not in seen:
            phrase, weight, family = _LOOKUP[key]
            matches.append({"phrase": phrase, "weight": weight, "family": family})
            seen.add(key)
    return matches


NEGATION = re.compile(
    r"\b(?:not|never|won't|will not|isn't|is not|doesn't|does not)\s+(?:be\s+)?"
    r"(?:on|in|for|part of)\s+(?:(?:the|your|our|this)\s+)?(?:exam|final|midterm)\b"
    r"|\bnot\s+(?:very\s+)?important\b|\b(?:no|not an?)\s+(?:homework|assignment)\b"
    r"|পরীক্ষায়\s+(?:এটা\s+)?(?:আসবে না|আসতে পারে না)"
    r"|গুরুত্বপূর্ণ\s+(?:নয়|না)|(?:হোমওয়ার্ক|অ্যাসাইনমেন্ট)\s+নেই"
)
STUDENT = re.compile(r"^\s*(?:student|ছাত্র|ছাত্রী|শিক্ষার্থী)\s*:", re.IGNORECASE)


def blocking_reason(text):
    if STUDENT.search(text):
        return "The source is explicitly labeled as a student statement, not teacher guidance."
    if NEGATION.search(normalize(text).replace("’", "'")):
        return "An explicit negative cue was found; conservatively excluded from important content."
    return ""


@dataclass(frozen=True)
class CandidateWindow:
    segment_id: int
    position: int
    text: str
    context: str
    keywords: list
    score: int
    start_time: float


def find_candidates(segments):
    """Split in memory; keep original source quotes and segment-level timestamps.

    Individual long sentences use overlapping 600-character windows. Context is
    bounded to 200 characters on either side, including adjacent segments when
    available. Evidence and timestamps always stay on the candidate segment.
    """
    segments = list(segments)
    for index, segment in enumerate(segments):
        for sentence in re.finditer(r"[^.!?।\n]+[.!?।]?", segment.text):
            left, right = sentence.span()
            while left < right:
                end = min(left + 600, right)
                text = segment.text[left:end]
                keywords = match_cues(text)
                if keywords:
                    before = segment.text[:left][-200:]
                    after = segment.text[end:end + 200]
                    if index and len(before) < 200:
                        before = (segments[index - 1].text + "\n" + before)[-200:]
                    if index + 1 < len(segments) and len(after) < 200:
                        after = (after + "\n" + segments[index + 1].text)[:200]
                    yield CandidateWindow(
                        segment.pk, left, text, before + text + after,
                        keywords, sum(item["weight"] for item in keywords), float(segment.start_time),
                    )
                if end == right:
                    break
                left = end - 80
