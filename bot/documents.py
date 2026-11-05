import logging
from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_CHARS = 40_000
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".py", ".html", ".xml", ".log"}
SUPPORTED_EXTENSIONS = {".pdf", ".docx", *TEXT_EXTENSIONS}


class DocumentError(Exception):
    pass


def is_supported(filename: str | None) -> bool:
    return bool(filename) and Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_text(filename: str, data: bytes) -> tuple[str, bool]:
    """Возвращает текст файла и признак того, что он был обрезан до MAX_CHARS."""
    extension = Path(filename).suffix.lower()
    try:
        if extension == ".pdf":
            reader = PdfReader(BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif extension == ".docx":
            document = Document(BytesIO(data))
            parts = [paragraph.text for paragraph in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            text = "\n".join(parts)
        elif extension in TEXT_EXTENSIONS:
            text = _decode_text(data)
        else:
            raise DocumentError("Я умею читать PDF, Word (.docx) и текстовые файлы (.txt).")
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("Не получилось прочитать файл: возможно, он повреждён или защищён паролем.") from exc

    text = text.strip()
    if not text:
        raise DocumentError("В файле не нашлось текста. Если это скан, пришлите страницы как фото — я их прочитаю.")
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS], True
    return text, False
