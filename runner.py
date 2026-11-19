import os
import sys
import glob
import asyncio
import logging
import shutil
from typing import Tuple, Optional, List

logger = logging.getLogger(__name__)


async def convert_to_pdf(docx_path: str, session_dir: str) -> Optional[str]:
    """
    Конвертирует docx в pdf с помощью headless LibreOffice / soffice.
    Возвращает путь к pdf или None в случае неудачи.
    """
    expected_pdf = os.path.splitext(docx_path)[0] + ".pdf"
    if os.path.exists(expected_pdf):
        try:
            os.remove(expected_pdf)
        except OSError:
            pass

    # Ищем исполняемый файл LibreOffice
    cmd = None
    for executable in ["libreoffice", "soffice"]:
        if shutil.which(executable):
            cmd = executable
            break

    if not cmd:
        logger.warning("LibreOffice не найден в системе. Пропуск генерации PDF.")
        return None

    try:
        logger.info(f"Конвертация {docx_path} в PDF через {cmd}...")
        proc = await asyncio.create_subprocess_exec(
            cmd,
            "--headless",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            session_dir,
            docx_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=35)
        if os.path.exists(expected_pdf) and os.path.getsize(expected_pdf) > 0:
            logger.info(f"PDF успешно создан: {expected_pdf}")
            return expected_pdf
    except Exception as e:
        logger.warning(f"Ошибка при конвертации в PDF: {e}")

    return None


async def run_generated_script(
    code: str,
    session_dir: str,
    timeout_seconds: int = 90
) -> Tuple[bool, Optional[str], Optional[str], List[str], Optional[str]]:
    """
    Записывает код в generate_report.py в session_dir и выполняет его.
    Возвращает: (успех: bool, docx_path, pdf_path, list_of_charts, error_msg)
    """
    os.makedirs(session_dir, exist_ok=True)
    script_path = os.path.join(session_dir, "generate_report.py")
    result_docx_path = os.path.join(session_dir, "result.docx")

    # Удаляем старый result.docx
    if os.path.exists(result_docx_path):
        try:
            os.remove(result_docx_path)
        except OSError:
            pass

    # Запоминаем существующие изображения до запуска скрипта
    existing_images = set(glob.glob(os.path.join(session_dir, "*.png")) + glob.glob(os.path.join(session_dir, "*.jpg")))

    # Сохраняем код скрипта
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(code)

    logger.info(f"Запуск скрипта генерации в директории: {session_dir}")

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "generate_report.py",
            cwd=session_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return False, None, None, [], f"Превышен лимит времени выполнения скрипта ({timeout_seconds} сек)."

        stdout = stdout_data.decode("utf-8", errors="replace")
        stderr = stderr_data.decode("utf-8", errors="replace")

        if process.returncode != 0:
            err_msg = f"Ошибка выполнения (код {process.returncode}):\n{stderr}\n{stdout}"
            logger.error(err_msg)
            return False, None, None, [], err_msg

        # Проверяем наличие итогового файла
        if not os.path.exists(result_docx_path) or os.path.getsize(result_docx_path) == 0:
            return False, None, None, [], "Скрипт завершился успешно, но файл 'result.docx' не был создан или пуст."

        # Находим новые сгенерированные графики/диаграммы
        current_images = set(glob.glob(os.path.join(session_dir, "*.png")) + glob.glob(os.path.join(session_dir, "*.jpg")))
        new_charts = sorted(list(current_images - existing_images))

        # Пытаемся конвертировать в PDF
        pdf_path = await convert_to_pdf(result_docx_path, session_dir)

        logger.info(f"Генерация завершена: DOCX={result_docx_path}, PDF={pdf_path}, Графиков={len(new_charts)}")
        return True, result_docx_path, pdf_path, new_charts, None

    except Exception as e:
        logger.exception("Исключение при запуске скрипта")
        return False, None, None, [], f"Системное исключение при запуске скрипта: {str(e)}"
