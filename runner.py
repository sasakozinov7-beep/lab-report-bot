import os
import sys
import asyncio
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

async def run_generated_script(
    code: str,
    session_dir: str,
    timeout_seconds: int = 90
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Записывает код в файл generate_report.py в session_dir и выполняет его.
    Возвращает: (успех: bool, путь к result.docx: str, ошибка/stderr: str)
    """
    os.makedirs(session_dir, exist_ok=True)
    script_path = os.path.join(session_dir, "generate_report.py")
    result_docx_path = os.path.join(session_dir, "result.docx")

    # Удаляем старый result.docx если он остался от прошлых запусков
    if os.path.exists(result_docx_path):
        try:
            os.remove(result_docx_path)
        except OSError:
            pass

    # Сохраняем код скрипта
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(code)

    logger.info(f"Запуск скрипта генерации в директории: {session_dir}")

    try:
        # Запускаем Python-процесс с текущим интерпретатором в папке session_dir
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
            return False, None, f"Превышен лимит времени выполнения скрипта ({timeout_seconds} сек)."

        stdout = stdout_data.decode("utf-8", errors="replace")
        stderr = stderr_data.decode("utf-8", errors="replace")

        if process.returncode != 0:
            err_msg = f"Ошибка выполнения (код возврата {process.returncode}):\n{stderr}\n{stdout}"
            logger.error(err_msg)
            return False, None, err_msg

        # Проверяем наличие итогового файла
        if not os.path.exists(result_docx_path) or os.path.getsize(result_docx_path) == 0:
            return False, None, "Скрипт завершился успешно, но файл 'result.docx' не был создан или пуст."

        logger.info(f"Файл успешно создан: {result_docx_path} ({os.path.getsize(result_docx_path)} байт)")
        return True, result_docx_path, None

    except Exception as e:
        logger.exception("Исключение при запуске скрипта")
        return False, None, f"Системное исключение при запуске скрипта: {str(e)}"
