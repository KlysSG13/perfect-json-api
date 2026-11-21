"""Модульная система экстракторов файлов с усиленной безопасностью.

Обработка идёт в памяти (BytesIO), временные файлы не создаются.
Тяжёлые парсеры выполняются через asyncio.to_thread.

Защита P0:
- проверка сигнатур (magic bytes) в дополнение к расширению;
- структурные лимиты: страницы PDF, параграфы DOCX, строки CSV,
  глубина/число ключей JSON, объём извлечённого текста;
- опциональная антивирусная проверка до парсинга (MALWARE_SCAN_ENABLED);
- опциональный OCR PDF-сканов (PDF_OCR_ENABLED) через pypdfium2+tesseract
  (зависимости не устанавливаются по умолчанию; при отсутствии —
  понятная ошибка pdf_ocr_unavailable).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from typing import Protocol

from app.config import Settings
from app.exceptions import (
    FileLimitsExceededError,
    FileTooLargeError,
    InvalidFileContentError,
    MalwareScannerUnavailableError,
    OCRUnavailableError,
    PDFOCRUnavailableError,
    STTUnavailableError,
    UnsupportedFileError,
)

logger = logging.getLogger(__name__)

MB = 1024 * 1024

TEXT_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".pdf", ".docx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".webm", ".flac"}

PDF_MIN_TEXT_CHARS = 20

SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "ogg": (b"OggS",),
    "flac": (b"fLaC",),
    "wav": (b"RIFF",),
    "webm": (b"\x1a\x45\xdf\xa3",),
}
AUDIO_TEXTUAL_HINTS = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


@dataclass
class ExtractionResult:
    text: str
    input_type: str
    warnings: list[str] = field(default_factory=list)


class MalwareScanner(Protocol):
    async def scan(self, content: bytes, filename: str) -> bool:
        """True — чисто; False — заражено (или бросить ошибку недоступности)."""
        ...


class ClamAVRestScanner:
    """Проверка через REST-обёртку ClamAV (например, clamd-rest: POST /scan)."""

    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")

    async def scan(self, content: bytes, filename: str) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self._url}/scan",
                    files={"file": (filename, content, "application/octet-stream")},
                )
        except httpx.HTTPError as exc:
            raise MalwareScannerUnavailableError(
                "Malware scanner is enabled but unreachable"
            ) from exc
        if response.status_code >= 500:
            raise MalwareScannerUnavailableError("Malware scanner returned a server error")
        payload = response.json() if response.content else {}
        infected = bool(payload.get("infected", False))
        if infected:
            raise InvalidFileContentError("Malware detected in uploaded file")
        return True


class FakeMalwareScanner:
    """Тестовый сканер: всегда чисто; считает вызовы."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def scan(self, content: bytes, filename: str) -> bool:
        self.calls.append(filename)
        return True


def build_malware_scanner(settings: Settings) -> MalwareScanner | None:
    if not settings.malware_scan_enabled:
        return None
    if not settings.clamav_url:
        raise MalwareScannerUnavailableError(
            "Malware scanning is enabled (MALWARE_SCAN_ENABLED) but CLAMAV_URL is not set"
        )
    return ClamAVRestScanner(settings.clamav_url)


def _matches_signature(content: bytes, kind: str) -> bool:
    prefixes = SIGNATURES.get(kind, ())
    return any(content.startswith(prefix) for prefix in prefixes)


def resolve_kind(filename: str, content_type: str | None) -> str:
    """Возвращает 'text' | 'image' | 'audio' | 'unknown' по расширению и MIME."""
    name = filename.lower()
    for ext in TEXT_EXTENSIONS:
        if name.endswith(ext):
            return "text"
    for ext in IMAGE_EXTENSIONS:
        if name.endswith(ext):
            return "image"
    for ext in AUDIO_EXTENSIONS:
        if name.endswith(ext):
            return "audio"
    mime = (content_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/") or mime in ("application/json",):
        return "text"
    return "unknown"


def _verify_signature(content: bytes, ext: str) -> None:
    """Проверяет соответствие содержимого расширению (magic bytes)."""
    if ext in ("", ".txt", ".md", ".csv", ".json"):
        for kind in ("pdf", "zip", "png", "jpeg", "gif"):
            if _matches_signature(content, kind):
                raise InvalidFileContentError(
                    f"File has a text extension ({ext or '.txt'}) but contains {kind.upper()} data"
                )
    if ext == ".pdf" and not _matches_signature(content, "pdf"):
        raise InvalidFileContentError("File has .pdf extension but is not a PDF document")
    if ext == ".docx" and not _matches_signature(content, "zip"):
        raise InvalidFileContentError("File has .docx extension but is not a DOCX (ZIP) document")
    if ext in (".png",) and not _matches_signature(content, "png"):
        raise InvalidFileContentError("File has .png extension but is not a PNG image")
    if ext in (".jpg", ".jpeg") and not _matches_signature(content, "jpeg"):
        raise InvalidFileContentError("File has .jpg extension but is not a JPEG image")
    if ext in (".ogg", ".oga") and not _matches_signature(content, "ogg"):
        raise InvalidFileContentError("File has .ogg extension but is not an OGG audio")
    if ext == ".flac" and not _matches_signature(content, "flac"):
        raise InvalidFileContentError("File has .flac extension but is not FLAC audio")
    if ext == ".webm" and not _matches_signature(content, "webm"):
        raise InvalidFileContentError("File has .webm extension but is not WebM")


def _looks_binary(text: str) -> bool:
    if "\x00" in text:
        return True
    if not text:
        return False
    replacement_count = text.count("\ufffd")
    return replacement_count / max(1, len(text)) > 0.30


async def extract_text_from_upload(
    content: bytes,
    filename: str,
    settings: Settings,
    content_type: str | None = None,
    scanner: MalwareScanner | None = None,
) -> ExtractionResult:
    """Главная точка входа: размер, антивирус, сигнатура, тип, извлечение."""
    max_bytes = settings.max_file_mb * MB
    if len(content) > max_bytes:
        raise FileTooLargeError(
            f"File is {len(content) / MB:.1f} MB, limit is {settings.max_file_mb} MB",
            details={"max_file_mb": settings.max_file_mb},
        )

    if scanner is not None:
        await scanner.scan(content, filename)

    kind = resolve_kind(filename, content_type)
    ext = _extension(filename)

    if kind == "image":
        return await _extract_image(content, ext, settings)
    if kind == "audio":
        return await _extract_audio(content, filename, content_type, settings)
    if kind == "text":
        _verify_signature(content, ext)
        if ext == ".pdf":
            return await _extract_pdf(content, settings)
        return await asyncio.to_thread(_extract_text_sync, content, ext, settings)
    raise UnsupportedFileError(
        "Unsupported file type",
        details={
            "filename": filename,
            "supported_extensions": sorted(TEXT_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS),
        },
    )


def _extension(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx >= 0 else ""


def _decode_bytes(content: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    try:
        return content.decode("utf-8"), warnings
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")
        warnings.append("File was not valid UTF-8; invalid bytes were replaced with U+FFFD.")
        return text, warnings


def _truncate_extracted(text: str, settings: Settings, warnings: list[str]) -> str:
    if len(text) > settings.max_extracted_chars:
        warnings.append(
            f"Extracted text was truncated from {len(text)} to {settings.max_extracted_chars} chars "
            "due to MAX_EXTRACTED_CHARS."
        )
        return text[: settings.max_extracted_chars]
    return text


def _reject_binary_text(text: str, ext: str) -> None:
    if _looks_binary(text):
        raise InvalidFileContentError(
            f"File has a text extension ({ext or 'txt'}) but contains binary data"
        )


def _extract_text_sync(content: bytes, ext: str, settings: Settings) -> ExtractionResult:
    if ext in ("", ".txt", ".md"):
        text, warnings = _decode_bytes(content)
        _reject_binary_text(text, ext or ".txt")
        return ExtractionResult(
            text=_truncate_extracted(text.strip(), settings, warnings),
            input_type=f"file{ext or '.txt'}",
            warnings=warnings,
        )
    if ext == ".json":
        return _extract_json_file(content, settings)
    if ext == ".csv":
        return _extract_csv(content, settings)
    if ext == ".docx":
        return _extract_docx(content, settings)
    raise UnsupportedFileError(f"Unsupported text file extension: {ext or '<none>'}")


def _extract_json_file(content: bytes, settings: Settings) -> ExtractionResult:
    text, warnings = _decode_bytes(content)
    _reject_binary_text(text, ".json")
    try:
        parsed = json.loads(text)
    except ValueError:
        warnings.append("File is not valid JSON; raw content passed as plain text.")
        return ExtractionResult(
            text=_truncate_extracted(text.strip(), settings, warnings),
            input_type="file.json",
            warnings=warnings,
        )
    depth = _json_depth(parsed)
    if depth > settings.max_json_depth:
        raise FileLimitsExceededError(
            f"JSON nesting depth {depth} exceeds MAX_JSON_DEPTH={settings.max_json_depth}",
            details={"limit": "max_json_depth", "actual": depth, "max": settings.max_json_depth},
        )
    keys = _json_keys(parsed)
    if keys > settings.max_json_keys:
        raise FileLimitsExceededError(
            f"JSON contains {keys} keys, MAX_JSON_KEYS={settings.max_json_keys}",
            details={"limit": "max_json_keys", "actual": keys, "max": settings.max_json_keys},
        )
    dumped = json.dumps(parsed, ensure_ascii=False, indent=2)
    return ExtractionResult(
        text=_truncate_extracted(dumped, settings, warnings),
        input_type="file.json",
        warnings=warnings,
    )


def _json_depth(node: object, depth: int = 1) -> int:
    if isinstance(node, dict):
        return max([depth] + [_json_depth(value, depth + 1) for value in node.values()])
    if isinstance(node, list):
        return max([depth] + [_json_depth(item, depth + 1) for item in node])
    return depth


def _json_keys(node: object) -> int:
    if isinstance(node, dict):
        return len(node) + sum(_json_keys(value) for value in node.values())
    if isinstance(node, list):
        return sum(_json_keys(item) for item in node)
    return 0


def _extract_csv(content: bytes, settings: Settings) -> ExtractionResult:
    text, warnings = _decode_bytes(content)
    _reject_binary_text(text, ".csv")
    rows: list[list[str]] = []
    limit_hit = False
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        rows.append(row)
        if len(rows) > settings.max_csv_rows:
            limit_hit = True
            break
    if limit_hit:
        raise FileLimitsExceededError(
            f"CSV exceeds MAX_CSV_ROWS={settings.max_csv_rows}",
            details={"limit": "max_csv_rows", "max": settings.max_csv_rows},
        )
    if not rows:
        return ExtractionResult(text="", input_type="file.csv", warnings=warnings + ["CSV file is empty."])
    header = rows[0]
    records: list[dict[str, str | None]] = []
    ragged = False
    for row in rows[1:]:
        if len(row) != len(header):
            ragged = True
        record: dict[str, str | None] = {}
        for i, key in enumerate(header):
            record[key] = row[i] if i < len(row) else None
        records.append(record)
    if ragged:
        warnings.append("CSV rows had a different number of columns than the header; missing cells set to null.")
    dumped = json.dumps(records, ensure_ascii=False, indent=2)
    return ExtractionResult(
        text=_truncate_extracted(dumped, settings, warnings),
        input_type="file.csv",
        warnings=warnings,
    )


async def _extract_pdf(content: bytes, settings: Settings) -> ExtractionResult:
    """Асинхронный PDF-путь: парсинг в потоке, при скане — OCR в потоке."""
    parsed = await asyncio.to_thread(_parse_pdf_sync, content, settings)
    if isinstance(parsed, ExtractionResult):
        return parsed
    # parsed — список предупреждений: текстового слоя нет, пробуем OCR
    warnings: list[str] = parsed  # type: ignore[assignment]
    if settings.pdf_ocr_enabled:
        return await _pdf_ocr(content, settings, warnings)
    raise UnsupportedFileError(
        "PDF has no extractable text layer (scanned document). "
        "Enable PDF_OCR_ENABLED=true (requires optional pypdfium2 + tesseract) "
        "or provide a text-based PDF."
    )


def _parse_pdf_sync(content: bytes, settings: Settings) -> ExtractionResult | list[str]:
    """Возвращает ExtractionResult при наличии текста либо список предупреждений."""
    from pypdf import PdfReader

    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise UnsupportedFileError("PDF is encrypted and cannot be read") from exc
            warnings.append("PDF was decrypted with an empty password.")
        page_count = len(reader.pages)
        if page_count > settings.max_pdf_pages:
            raise FileLimitsExceededError(
                f"PDF has {page_count} pages, MAX_PDF_PAGES={settings.max_pdf_pages}",
                details={"limit": "max_pdf_pages", "actual": page_count, "max": settings.max_pdf_pages},
            )
        pages_text: list[str] = []
        for page in reader.pages:
            try:
                pages_text.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - одна битая страница не роняет файл
                warnings.append("One PDF page could not be parsed and was skipped.")
        text = "\n".join(pages_text).strip()
    except FileLimitsExceededError:
        raise
    except UnsupportedFileError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InvalidFileContentError(f"PDF could not be parsed: {type(exc).__name__}") from exc

    if len(text) < PDF_MIN_TEXT_CHARS:
        return warnings
    return ExtractionResult(
        text=_truncate_extracted(text, settings, warnings),
        input_type="file.pdf",
        warnings=warnings,
    )


def _pdf_ocr_run(content: bytes, settings: Settings) -> str:
    import pytesseract
    from pypdfium2 import PdfDocument

    pdf = PdfDocument(io.BytesIO(content))
    parts: list[str] = []
    for index in range(min(len(pdf), settings.max_pdf_pages)):
        page = pdf[index]
        bitmap = page.render(scale=2.0)
        image = bitmap.to_pil()
        parts.append(pytesseract.image_to_string(image, lang=settings.ocr_lang))
    return "\n".join(parts).strip()


async def _pdf_ocr(content: bytes, settings: Settings, warnings: list[str]) -> ExtractionResult:
    """OCR-растеризация PDF-скана (опциональные зависимости)."""
    try:
        import importlib

        importlib.import_module("pypdfium2")
    except ImportError as exc:
        raise PDFOCRUnavailableError(
            "PDF scan OCR requires the optional pypdfium2 package; "
            "install it (and tesseract) or disable PDF_OCR_ENABLED"
        ) from exc
    if not settings.ocr_enabled:
        raise PDFOCRUnavailableError(
            "PDF_OCR_ENABLED=true requires OCR_ENABLED=true and a tesseract binary"
        )
    try:
        import pytesseract  # noqa: F401

        pytesseract.get_tesseract_version()
    except ImportError as exc:
        raise PDFOCRUnavailableError(
            "PDF scan OCR requires optional dependencies (pytesseract, tesseract binary); "
            "install them or disable PDF_OCR_ENABLED"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise PDFOCRUnavailableError(
            "tesseract binary not found on PATH; required for PDF_OCR_ENABLED"
        ) from exc

    try:
        text = await asyncio.to_thread(_pdf_ocr_run, content, settings)
    except Exception as exc:  # noqa: BLE001
        raise PDFOCRUnavailableError(f"PDF OCR failed: {type(exc).__name__}") from exc
    if not text.strip():
        warnings.append("PDF OCR returned no text.")
    return ExtractionResult(
        text=_truncate_extracted(text.strip(), settings, warnings),
        input_type="file.pdf.ocr",
        warnings=warnings,
    )


def _extract_docx(content: bytes, settings: Settings) -> ExtractionResult:
    import docx

    warnings: list[str] = []
    try:
        if not zipfile.is_zipfile(io.BytesIO(content)):
            raise InvalidFileContentError("DOCX is not a valid ZIP container")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if not any(name.startswith("word/") for name in archive.namelist()):
                raise InvalidFileContentError("DOCX archive does not contain word/ documents")
        document = docx.Document(io.BytesIO(content))
    except InvalidFileContentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InvalidFileContentError(f"DOCX could not be parsed: {type(exc).__name__}") from exc

    paragraphs = document.paragraphs
    if len(paragraphs) > settings.max_docx_paragraphs:
        raise FileLimitsExceededError(
            f"DOCX has {len(paragraphs)} paragraphs, MAX_DOCX_PARAGRAPHS={settings.max_docx_paragraphs}",
            details={"limit": "max_docx_paragraphs", "actual": len(paragraphs), "max": settings.max_docx_paragraphs},
        )

    parts: list[str] = [p.text.strip() for p in paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        warnings.append("DOCX contains no extractable text (only images or empty paragraphs).")
    return ExtractionResult(
        text=_truncate_extracted(text, settings, warnings),
        input_type="file.docx",
        warnings=warnings,
    )


async def _extract_image(content: bytes, ext: str, settings: Settings) -> ExtractionResult:
    if not settings.ocr_enabled:
        raise UnsupportedFileError(
            "Image files are only supported when OCR is enabled (OCR_ENABLED=true)."
        )
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - зависимости объявлены в requirements
        raise OCRUnavailableError("OCR dependencies (pytesseract/Pillow) are not installed") from exc

    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # noqa: BLE001
        raise OCRUnavailableError(
            "tesseract binary not found on PATH; install tesseract-ocr to use OCR"
        ) from exc

    def _run_ocr() -> str:
        image = Image.open(io.BytesIO(content))
        return pytesseract.image_to_string(image, lang=settings.ocr_lang)

    try:
        text = await asyncio.to_thread(_run_ocr)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).strip() or type(exc).__name__
        raise OCRUnavailableError(f"OCR failed: {message[:200]}") from exc
    warnings = [] if text.strip() else ["OCR returned no text."]
    return ExtractionResult(
        text=_truncate_extracted(text.strip(), settings, warnings),
        input_type=f"file{ext}",
        warnings=warnings,
    )


async def _extract_audio(
    content: bytes,
    filename: str,
    content_type: str | None,
    settings: Settings,
) -> ExtractionResult:
    if not settings.stt_enabled:
        raise UnsupportedFileError(
            "Audio files are only supported when STT is enabled (STT_ENABLED=true)."
        )
    if not settings.stt_api_key:
        raise STTUnavailableError("STT is enabled but STT_API_KEY is not set")
    if settings.stt_provider != "openai":
        raise STTUnavailableError(
            f"STT_PROVIDER '{settings.stt_provider}' is not supported; only 'openai' (compatible) is"
        )

    import httpx

    url = settings.llm_base_url.rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {settings.stt_api_key}"}
    files = {"file": (filename, content, content_type or "application/octet-stream")}
    data = {"model": settings.stt_model}

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(url, headers=headers, files=files, data=data)
            response.raise_for_status()
            payload = response.json()
            text = payload.get("text", "")
    except httpx.HTTPError as exc:
        raise STTUnavailableError(f"STT provider request failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise STTUnavailableError("STT provider returned a malformed response") from exc

    return ExtractionResult(
        text=_truncate_extracted(str(text).strip(), settings, []),
        input_type="file.audio",
        warnings=[] if str(text).strip() else ["STT returned no text."],
    )
