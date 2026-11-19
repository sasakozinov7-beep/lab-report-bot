import os
import re
import asyncio
import zipfile
import logging
from pypdf import PdfReader
import docx

logger = logging.getLogger(__name__)

def is_image_file(file_path: str) -> bool:
    ext = os.path.splitext(file_path)[1].lower()
    return ext in [".jpg", ".jpeg", ".png", ".webp", ".bmp"]

async def extract_text_from_file(file_path: str, max_chars: int = 80000, status_callback=None) -> str:
    """Извлекает текст из файлов PDF, DOCX, MD, TXT или изображений (JPG, PNG, WEBP)."""
    ext = os.path.splitext(file_path)[1].lower()

    if is_image_file(file_path):
        from llm_service import llm_service
        return await llm_service.extract_text_from_image(file_path)
    elif ext == ".pdf":
        return await _extract_from_pdf(file_path, max_chars, status_callback)
    elif ext == ".docx":
        return await _extract_from_docx(file_path, max_chars, status_callback)
    elif ext in [".txt", ".md"]:
        return _extract_from_text(file_path, max_chars)
    else:
        try:
            return _extract_from_text(file_path, max_chars)
        except Exception:
            raise ValueError(f"Неподдерживаемый формат файла: {ext}. Поддерживаются .pdf, .docx, .md, .txt и фото (.jpg, .png)")

async def _extract_from_pdf(file_path: str, max_chars: int, status_callback=None) -> str:
    reader = PdfReader(file_path)
    text_parts = []
    total_len = 0
    pdf_images = []
    temp_dir = os.path.join(os.path.dirname(file_path), "pdf_images")
    os.makedirs(temp_dir, exist_ok=True)

    for page_idx, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        if page_text.strip():
            text_parts.append(f"--- Страница {page_idx + 1} ---\n" + page_text)
            total_len += len(page_text)
        
        # Проверяем наличие встроенных картинок на случай скана
        if hasattr(page, "images"):
            for img_idx, img in enumerate(page.images):
                if len(img.data) > 5000:
                    img_path = os.path.join(temp_dir, f"pdf_p{page_idx}_{img_idx}_{img.name}")
                    with open(img_path, "wb") as f_img:
                        f_img.write(img.data)
                    pdf_images.append(img_path)

        if total_len >= max_chars:
            text_parts.append("\n[...Текст обрезан из-за превышения максимального объема...]")
            break

    full_text = "\n\n".join(text_parts).strip()

    # Если текста достаточно (>250 симв.), возвращаем мгновенно
    if len(full_text) >= 250:
        return full_text

    # Если текста почти нет (сканированный PDF), запускаем Vision OCR
    if pdf_images:
        logger.info(f"В PDF мало текста ({len(full_text)} симв.), но найдено {len(pdf_images)} сканов. Запуск Vision OCR...")
        if status_callback:
            try:
                await status_callback("📸 Распознаю страницы сканированного PDF...")
            except Exception:
                pass
        from llm_service import llm_service
        ocr_text = await llm_service.extract_text_from_images(pdf_images[:8])
        if full_text:
            full_text = f"{full_text}\n\n--- [Текст, распознанный со сканов в PDF] ---\n{ocr_text}"
        else:
            full_text = ocr_text

    return full_text

async def _extract_from_docx(file_path: str, max_chars: int, status_callback=None) -> str:
    # 1. Мгновенное извлечение обычных параграфов и таблиц
    text_parts = []
    try:
        doc = docx.Document(file_path)
        for p in doc.paragraphs:
            if p.text.strip():
                text_parts.append(p.text.strip())
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    text_parts.append(row_text)
    except Exception as e:
        logger.warning(f"Ошибка при разборе параграфов docx: {e}")

    text = "\n".join(text_parts).strip()

    # Скоростная оптимизация: если в документе уже есть нормальный текст (>250 симв.),
    # отдаем его сразу за 0.05 сек без сканирования архива и OCR!
    if len(text) >= 250:
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[...Текст обрезан из-за превышения максимального объема...]"
        return text

    # 2. Если текста нет или мало (<250 симв.) — извлекаем вложенные скриншоты из word/media/
    extracted_images = []
    temp_img_dir = os.path.join(os.path.dirname(file_path), "docx_images")
    os.makedirs(temp_img_dir, exist_ok=True)

    try:
        with zipfile.ZipFile(file_path, "r") as z:
            media_names = [n for n in z.namelist() if n.startswith("word/media/")]
            def natural_sort_key(s):
                return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', s)]
            media_names.sort(key=natural_sort_key)

            for name in media_names:
                ext_img = os.path.splitext(name)[1].lower()
                if ext_img in [".png", ".jpg", ".jpeg", ".webp", ".bmp"]:
                    data = z.read(name)
                    if len(data) > 3000 or len(media_names) == 1:
                        out_name = f"{os.path.basename(file_path)}_{os.path.basename(name)}"
                        out_path = os.path.join(temp_img_dir, out_name)
                        with open(out_path, "wb") as f_out:
                            f_out.write(data)
                        extracted_images.append(out_path)
    except Exception as zip_err:
        logger.warning(f"Ошибка чтения zip-структуры docx: {zip_err}")

    # 3. Распознаем фото заданий через Vision OCR
    if extracted_images:
        logger.info(f"DOCX содержит мало текста ({len(text)} симв.), но найдено {len(extracted_images)} картинок. Запуск Vision OCR...")
        if status_callback:
            try:
                await status_callback("📸 Распознаю задание со скриншотов в документе...")
            except Exception:
                pass
        from llm_service import llm_service
        ocr_text = await llm_service.extract_text_from_images(extracted_images)
        if text:
            text = f"{text}\n\n--- [Текст задания, распознанный с фото/скриншотов в DOCX] ---\n{ocr_text}"
        else:
            text = ocr_text

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
