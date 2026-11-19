import os
from pypdf import PdfReader
import docx

def extract_text_from_file(file_path: str, max_chars: int = 80000) -> str:
    """Извлекает текст из файлов PDF, DOCX, MD или TXT."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        return _extract_from_pdf(file_path, max_chars)
    elif ext == ".docx":
        return _extract_from_docx(file_path, max_chars)
    elif ext in [".txt", ".md"]:
        return _extract_from_text(file_path, max_chars)
    else:
        # Попытка прочитать как текст
        try:
            return _extract_from_text(file_path, max_chars)
        except Exception as e:
            raise ValueError(f"Неподдерживаемый формат файла: {ext}. Поддерживаются .pdf, .docx, .md, .txt")

def _extract_from_pdf(file_path: str, max_chars: int) -> str:
    reader = PdfReader(file_path)
    text_parts = []
    total_len = 0
    for page_idx, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        text_parts.append(f"--- Страница {page_idx + 1} ---\n" + page_text)
        total_len += len(page_text)
        if total_len >= max_chars:
            text_parts.append("\n[...Текст обрезан из-за превышения максимального объема...]")
            break
    return "\n\n".join(text_parts)

def _extract_from_docx(file_path: str, max_chars: int) -> str:
    doc = docx.Document(file_path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    text = "\n".join(paragraphs)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[...Текст обрезан из-за превышения максимального объема...]"
    return text

def _extract_from_text(file_path: str, max_chars: int) -> str:
    encodings = ["utf-8", "cp1251", "latin-1"]
    content = ""
    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc) as f:
                content = f.read()
                break
        except UnicodeDecodeError:
            continue
    if not content:
        raise ValueError("Не удалось прочитать текстовый файл (ошибка кодировки).")
    
    if len(content) > max_chars:
        content = content[:max_chars] + "\n[...Текст обрезан из-за превышения максимального объема...]"
    return content
