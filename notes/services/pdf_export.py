"""Local Unicode PDF notes, rendered with Pango/HarfBuzz via WeasyPrint."""
import json
import logging
from pathlib import Path
import shutil
import subprocess

from django.conf import settings
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string

from .chunking import fingerprint, transcript_source
from .schema import validate_note_data

logger = logging.getLogger(__name__)


class PDFExportError(Exception):
    pass


def _bengali_codepoints(text):
    return {ord(character) for character in text if 0x0980 <= ord(character) <= 0x09FF}


def _font_supports(path, codepoints):
    try:
        from fontTools.ttLib import TTFont
        with TTFont(str(path)) as font:
            return codepoints.issubset(font.getBestCmap() or {})
    except Exception as exc:
        raise PDFExportError("The PDF font could not be read. Configure a compatible TTF or OTF font.") from exc


def select_font(text):
    """Prefer configured font or free system fonts; never download or bundle fonts."""
    required = _bengali_codepoints(text)
    configured = settings.PDF_FONT_PATH
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise PDFExportError("PDF_FONT_PATH does not point to an available font file.")
        if not _font_supports(path, required):
            raise PDFExportError("The configured PDF font does not cover the Bangla characters in these notes.")
        return path
    candidates = [
        Path("/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSerifBengali-Regular.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSerif.ttf"),
    ]
    if required and shutil.which("fc-match"):
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", "Noto Sans Bengali:lang=bn"],
                capture_output=True, text=True, check=True, timeout=3,
            )
            if result.stdout.strip():
                candidates.append(Path(result.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            logger.debug("System Bangla font lookup unavailable.")
    for path in candidates:
        if path.is_file() and _font_supports(path, required):
            return path.resolve()
    if required:
        raise PDFExportError("No compatible Bangla font found. Install Noto Sans Bengali or set PDF_FONT_PATH.")
    # Pango's open system Latin font fallback is sufficient for English-only notes.
    return None


def restricted_font_fetcher(allowed, default_fetcher):
    def fetch(url, *args, **kwargs):
        if url not in allowed:
            raise PDFExportError("PDF export only permits its configured local font resources.")
        return default_fetcher(url, *args, **kwargs)
    return fetch


def render_notes_pdf(lecture, note):
    try:
        from weasyprint import HTML, default_url_fetcher
        from weasyprint.text.fonts import FontConfiguration
    except (ImportError, OSError) as exc:
        raise PDFExportError("PDF rendering is unavailable. Install the PDF dependencies and Pango libraries.") from exc
    try:
        validate_note_data(note.data)
    except ValidationError as exc:
        raise PDFExportError("Saved notes are invalid. Regenerate them before exporting.") from exc
    # Include metadata when checking font coverage, not only the generated note body.
    text = f"{lecture.course.course_code} {lecture.course.course_name} {lecture.title} " + json.dumps(
        note.data, ensure_ascii=False,
    )
    font = select_font(text)
    uri = font.as_uri() if font else ""
    stale = note.transcript_fingerprint != fingerprint(transcript_source(lecture.transcript_segments.all()))
    html = render_to_string("notes/pdf/lecture_notes.html", {
        "lecture": lecture, "data": note.data, "font_uri": uri, "stale": stale,
    })
    try:
        pdf = HTML(
            string=html,
            url_fetcher=restricted_font_fetcher({uri} if uri else set(), default_url_fetcher),
        ).write_pdf(font_config=FontConfiguration())
    except Exception as exc:
        logger.exception("PDF rendering failed for lecture %s.", lecture.pk)
        raise PDFExportError("The PDF could not be rendered. Check the configured font and PDF libraries.") from exc
    logger.info("Exported note PDF for lecture %s.", lecture.pk)
    return pdf
