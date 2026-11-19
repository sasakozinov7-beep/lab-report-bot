import os
import shutil
import logging
from typing import Dict, Any

from aiogram import Router, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command, CommandStart

from config import config
from file_extractor import extract_text_from_file
from runner import run_generated_script
from llm_service import llm_service

logger = logging.getLogger(__name__)
router = Router()

# Хранилище сессий пользователей в оперативной памяти
# Структура: {user_id: {"lab_text": str, "last_code": str, "session_dir": str, "last_docx": str}}
user_sessions: Dict[int, Dict[str, Any]] = {}

BASE_SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(BASE_SESSIONS_DIR, exist_ok=True)


def get_user_dir(user_id: int) -> str:
    path = os.path.join(BASE_SESSIONS_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


@router.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = (
        "👋 **Привет! Я бот для генерации академических отчетов по лабораторным и практическим работам.**\n\n"
        "📄 **Как со мной работать:**\n"
        "1. Отправь мне **файл с методичкой** (`.pdf`, `.docx`, `.txt`, `.md`).\n"
        "2. В подписи к файлу (или следующим сообщением) укажи требования:\n"
        "   • ФИО студента и учебную группу\n"
        "   • Дисциплину и ФИО преподавателя\n"
        "   • Особенности оформления (например: *«сделай титульник, диаграммы, таблицы, формулы, убери теорию, ответы на вопросы в конец»*).\n\n"
        "✨ **Что я сделаю:**\n"
        "• Проанализирую методичку и решу задачи\n"
        "• Нарисую нужные схемы и диаграммы (через matplotlib)\n"
        "• Оформлю аккуратные формулы Word (OMML)\n"
        "• Соберу идеальный документ `.docx` по ГОСТу\n\n"
        "🔄 **Итеративные правки:**\n"
        "Если после получения файла нужно что-то скорректировать — просто напиши свои замечания в чат (например: *«исправь формулы»* или *«перенеси вопросы в конец»*), и я обновлю документ!\n\n"
        "Для сброса контекста используй команду /reset."
    )
    await message.answer(welcome_text, parse_mode="Markdown")


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    user_id = message.from_user.id
    if user_id in user_sessions:
        del user_sessions[user_id]
    user_dir = get_user_dir(user_id)
    if os.path.exists(user_dir):
        try:
            shutil.rmtree(user_dir)
        except Exception:
            pass
    await message.answer("🧹 **Сессия успешно сброшена.** Отправьте новый файл с заданием для начала работы.")


@router.message(F.document)
async def handle_document(message: Message):
    """Обработка загрузки файла с заданием."""
    user_id = message.from_user.id
    doc = message.document
    filename = doc.file_name or "uploaded_file"
    ext = os.path.splitext(filename)[1].lower()

    if ext not in [".pdf", ".docx", ".txt", ".md"]:
        await message.answer(
            "⚠️ Пожалуйста, отправьте файл в формате `.pdf`, `.docx`, `.txt` или `.md`."
        )
        return

    user_dir = get_user_dir(user_id)
    local_file_path = os.path.join(user_dir, filename)

    status_msg = await message.answer(f"📥 Скачиваю и анализирую файл `{filename}`...")

    try:
        # Скачиваем файл из Telegram
        await message.bot.download(doc, destination=local_file_path)

        # Извлекаем текст
        extracted_text = extract_text_from_file(local_file_path, max_chars=config.MAX_PROMPT_CHARS)

        if not extracted_text.strip():
            await status_msg.edit_text("❌ Не удалось извлечь текст из файла. Возможно, это скан или пустой документ.")
            return

        # Инициализируем сессию пользователя
        user_sessions[user_id] = {
            "lab_text": extracted_text,
            "session_dir": user_dir,
            "last_code": "",
            "last_docx": ""
        }

        # Проверяем, есть ли подпись к файлу с инструкциями
        instructions = message.caption or ""
        if instructions.strip():
            await status_msg.edit_text("📝 Требования получены из подписи к файлу. Начинаю генерацию...")
            await process_generation(message, status_msg, user_id, instructions)
        else:
            await status_msg.edit_text(
                "✅ **Методические указания успешно прочитаны!**\n\n"
                "Теперь напишите ваши пожелания к отчету:\n"
                "• ФИО студента, группу, специальность\n"
                "• ФИО преподавателя\n"
                "• Особые требования (например: *«титульник МосТеха, убрать теорию, формулы красивыми, диаграмму процесса»*)\n\n"
                "*(или просто напишите «Сделай по стандарту» для типового отчета)*"
            )

    except Exception as e:
        logger.exception("Ошибка при обработке документа")
        await status_msg.edit_text(f"❌ Произошла ошибка при чтении файла:\n`{str(e)}`")


@router.message(F.text)
async def handle_text_message(message: Message):
    """Обработка текстовых сообщений (пожелания к новой лабе или правки к готовой)."""
    user_id = message.from_user.id
    user_text = message.text.strip()

    if user_id not in user_sessions or not user_sessions[user_id].get("lab_text"):
        await message.answer(
            "ℹ️ Сначала отправьте файл с заданием лабораторной работы (`.pdf`, `.docx`, `.md`)."
        )
        return

    session = user_sessions[user_id]
    has_previous_code = bool(session.get("last_code"))

    if not has_previous_code:
        # Это первичные инструкции к только что загруженному файлу
        status_msg = await message.answer("⏳ Принято! Формирую структуру работы и начинаю генерацию...")
        await process_generation(message, status_msg, user_id, user_text)
    else:
        # Это запрос на редактирование уже существующего отчета
        status_msg = await message.answer("🔄 Вношу указанные правки в отчет...")
        await process_edit(message, status_msg, user_id, user_text)


async def process_generation(message: Message, status_msg: Message, user_id: int, instructions: str):
    """Генерация первичного отчета с авто-исправлением ошибок."""
    session = user_sessions[user_id]
    lab_text = session["lab_text"]
    user_dir = session["session_dir"]

    try:
        await status_msg.edit_text("🧠 Нейросеть решает задания и пишет программный код сборки документа...")
        code = await llm_service.generate_initial_code(lab_text, instructions)

        # Выполняем скрипт (с авто-исправлением до 2 попыток)
        success, docx_path, error_msg = await execute_with_auto_fix(code, user_dir, status_msg)

        if success and docx_path and os.path.exists(docx_path):
            session["last_code"] = code
            session["last_docx"] = docx_path
            await send_completed_file(message, status_msg, docx_path)
        else:
            await status_msg.edit_text(
                f"❌ Не удалось успешно скомпилировать документ:\n\n`{error_msg[:1000]}`\n\n"
                "Попробуйте уточнить запрос или отправьте /reset."
            )

    except Exception as e:
        logger.exception("Ошибка в процессе генерации")
        await status_msg.edit_text(f"❌ Критическая ошибка генерации:\n`{str(e)}`")


async def process_edit(message: Message, status_msg: Message, user_id: int, edit_instructions: str):
    """Внесение правок в ранее сгенерированный документ."""
    session = user_sessions[user_id]
    old_code = session["last_code"]
    user_dir = session["session_dir"]

    try:
        await status_msg.edit_text("🧠 Анализирую замечания и обновляю скрипт сборки...")
        new_code = await llm_service.edit_existing_code(old_code, edit_instructions)

        # Выполняем обновленный скрипт
        success, docx_path, error_msg = await execute_with_auto_fix(new_code, user_dir, status_msg)

        if success and docx_path and os.path.exists(docx_path):
            session["last_code"] = new_code
            session["last_docx"] = docx_path
            await send_completed_file(message, status_msg, docx_path, is_update=True)
        else:
            await status_msg.edit_text(
                f"❌ Ошибка при сборке обновленного документа:\n\n`{error_msg[:1000]}`"
            )

    except Exception as e:
        logger.exception("Ошибка при редактировании документа")
        await status_msg.edit_text(f"❌ Ошибка при внесении правок:\n`{str(e)}`")


async def execute_with_auto_fix(code: str, user_dir: str, status_msg: Message, max_attempts: int = 2):
    """Выполняет сгенерированный код и при возникновении ошибок просит LLM исправить их."""
    current_code = code

    for attempt in range(1, max_attempts + 1):
        await status_msg.edit_text(f"⚙️ Выполняю сборку документа (попытка {attempt})...")
        success, docx_path, error_msg = await run_generated_script(
            current_code,
            user_dir,
            timeout_seconds=config.RUNNER_TIMEOUT_SECONDS
        )

        if success:
            return True, docx_path, None

        if attempt < max_attempts:
            await status_msg.edit_text("🔧 Обнаружена техническая ошибка. Нейросеть автоматически исправляет код...")
            current_code = await llm_service.fix_broken_code(current_code, error_msg or "Unknown error")

    return False, None, error_msg


async def send_completed_file(message: Message, status_msg: Message, docx_path: str, is_update: bool = False):
    """Отправка сгенерированного файла пользователю."""
    try:
        await status_msg.delete()
    except Exception:
        pass

    action_word = "обновлен" if is_update else "готов"
    caption = (
        f"✅ **Ваш отчет успешно {action_word}!**\n\n"
        "📎 Формат: Microsoft Word (.docx)\n"
        "📐 Оформление: ГОСТ (поля 30/15/20/20, шрифт Times New Roman, таблицы, формулы OMML)\n\n"
        "💬 **Нужны правки?** Просто ответьте на это сообщение (например: *«исправь формулу во 2 вопросе»*, *«замени преподавателя на Иванова»*, *«добавь еще одну диаграмму»*)."
    )

    file_to_send = FSInputFile(docx_path, filename="Laboratornaya_Rabota.docx")
    await message.answer_document(document=file_to_send, caption=caption, parse_mode="Markdown")
