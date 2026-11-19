import os
import shutil
import logging
from typing import Dict, Any, Optional, List

from aiogram import Router, F
from aiogram.types import (
    Message, 
    FSInputFile, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from aiogram.filters import Command, CommandStart

from config import config
from file_extractor import extract_text_from_file
from runner import run_generated_script
from llm_service import llm_service
from database import (
    get_profile, 
    update_profile, 
    add_lab_history, 
    get_user_labs
)

logger = logging.getLogger(__name__)
router = Router()

# Хранилище сессий пользователей в оперативной памяти
user_sessions: Dict[int, Dict[str, Any]] = {}

BASE_SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(BASE_SESSIONS_DIR, exist_ok=True)


def get_user_dir(user_id: int) -> str:
    path = os.path.join(BASE_SESSIONS_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


# -------------------------------------------------------------
# Функции чистого интерфейса (Auto-Clean)
# -------------------------------------------------------------
async def safe_delete_msg(bot, chat_id: int, message_id: int) -> None:
    """Безопасное удаление одного сообщения."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def track_message(user_id: int, message_id: int) -> None:
    """Добавление сообщения в стек для последующей очистки."""
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    if "clean_stack" not in user_sessions[user_id]:
        user_sessions[user_id]["clean_stack"] = []
    user_sessions[user_id]["clean_stack"].append(message_id)


async def clear_old_messages(bot, chat_id: int, user_id: int, keep_ids: Optional[List[int]] = None) -> None:
    """Удаляет все предыдущие промежуточные сообщения бота и пользователя."""
    if user_id not in user_sessions:
        return
    stack = user_sessions[user_id].get("clean_stack", [])
    keep = set(keep_ids or [])
    for mid in stack:
        if mid not in keep:
            await safe_delete_msg(bot, chat_id, mid)
    user_sessions[user_id]["clean_stack"] = [mid for mid in stack if mid in keep]


async def safe_edit(msg: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Безопасное редактирование сообщения."""
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        try:
            await msg.answer(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            pass


# -------------------------------------------------------------
# Клавиатуры
# -------------------------------------------------------------
def get_options_keyboard(profile: Dict[str, Any], custom_text: str = "") -> InlineKeyboardMarkup:
    """Интерактивная клавиатура параметров отчета."""
    t_title = "✅ Вкл" if profile.get("include_title", 1) else "❌ Выкл"
    t_quest = "✅ Вкл" if profile.get("include_questions", 1) else "❌ Выкл"
    t_theor = "✅ Вкл" if profile.get("omit_theory", 1) else "❌ Выкл"
    t_chart = "✅ Вкл" if profile.get("include_charts", 1) else "❌ Выкл"
    t_anti = "✅ Вкл" if profile.get("antiplagiat_mode", 1) else "❌ Выкл"
    variant = profile.get("variant") or "Любой"

    custom_label = f"✏️ Пожелания: {custom_text[:15]}..." if custom_text else "✏️ Особые пожелания текстом"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🎓 Титульник: {t_title}", callback_data="toggle_title"),
                InlineKeyboardButton(text=f"❓ Вопросы: {t_quest}", callback_data="toggle_quest")
            ],
            [
                InlineKeyboardButton(text=f"📉 Без теории: {t_theor}", callback_data="toggle_theor"),
                InlineKeyboardButton(text=f"📊 Графики: {t_chart}", callback_data="toggle_chart")
            ],
            [
                InlineKeyboardButton(text=f"🛡️ Антиплагиат: {t_anti}", callback_data="toggle_anti"),
                InlineKeyboardButton(text=f"🎯 Вариант: {variant}", callback_data="set_variant")
            ],
            [
                InlineKeyboardButton(text=custom_label, callback_data="enter_custom_text")
            ],
            [
                InlineKeyboardButton(text="🚀 Сгенерировать отчет", callback_data="start_generation")
            ]
        ]
    )
    return keyboard


def get_profile_keyboard(profile: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Клавиатура редактирования профиля."""
    anti = "ВКЛ" if profile.get("antiplagiat_mode", 1) else "ВЫКЛ"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ ФИО Студента", callback_data="prof_full_name"),
                InlineKeyboardButton(text="✏️ Учебная группа", callback_data="prof_group_name")
            ],
            [
                InlineKeyboardButton(text="✏️ ВУЗ / Кафедра", callback_data="prof_university"),
                InlineKeyboardButton(text="✏️ Преподаватель", callback_data="prof_teacher")
            ],
            [
                InlineKeyboardButton(text="✏️ Дисциплина", callback_data="prof_discipline"),
                InlineKeyboardButton(text="🎯 Вариант", callback_data="prof_variant")
            ],
            [
                InlineKeyboardButton(text=f"🛡️ Антиплагиат: {anti}", callback_data="toggle_anti_prof")
            ],
            [
                InlineKeyboardButton(text="🔄 Закрыть профиль", callback_data="close_profile")
            ]
        ]
    )
    return keyboard


def get_cancel_keyboard(back_to: str = "profile") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Отмена", callback_data=f"cancel_{back_to}")
        ]]
    )


# -------------------------------------------------------------
# Команды
# -------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    # Удаляем входящую команду пользователя и предыдущие сообщения бота
    await safe_delete_msg(bot, chat_id, message.message_id)
    await clear_old_messages(bot, chat_id, user_id)

    welcome_text = (
        "👋 **Привет! Я бот для быстрой сборки академических отчетов по ГОСТу.**\n\n"
        "📄 **Как получить готовый отчет:**\n"
        "1. Отправь **файл методички** (`.pdf`, `.docx`, `.txt`) или **фото задания**.\n"
        "2. Укажи свои требования или нажми **«Сгенерировать отчет»**.\n"
        "3. Забирай готовый документ в **DOCX** и **PDF** с решением, таблицами, формулами и графиками!\n\n"
        "⚙️ Введи /profile, чтобы один раз сохранить свои ФИО, группу и препода."
    )
    sent = await message.answer(welcome_text, parse_mode="Markdown")
    track_message(user_id, sent.message_id)


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await safe_delete_msg(bot, chat_id, message.message_id)
    await clear_old_messages(bot, chat_id, user_id)

    prof = get_profile(user_id)
    anti_str = "ВКЛ (высокая оригинальность)" if prof.get("antiplagiat_mode", 1) else "ВЫКЛ"

    text = (
        "👤 **Ваш студенческий профиль:**\n\n"
        f"• **ФИО студента:** {prof.get('full_name') or '*(не указано)*'}\n"
        f"• **Группа:** {prof.get('group_name') or '*(не указано)*'}\n"
        f"• **ВУЗ / Кафедра:** {prof.get('university') or '*(не указано)*'}\n"
        f"• **Преподаватель:** {prof.get('teacher') or '*(не указано)*'}\n"
        f"• **Дисциплина:** {prof.get('discipline') or '*(не указано)*'}\n"
        f"• **Вариант по умолчанию:** {prof.get('variant') or '*(любой)*'}\n"
        f"• **Режим Антиплагиат:** {anti_str}\n\n"
        "💡 *Эти данные автоматически подставляются в титульный лист.*\n"
        "Нажмите кнопку ниже, чтобы изменить нужное поле:"
    )
    sent = await message.answer(text, reply_markup=get_profile_keyboard(prof), parse_mode="Markdown")
    track_message(user_id, sent.message_id)


@router.message(Command("my_labs"))
async def cmd_my_labs(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await safe_delete_msg(bot, chat_id, message.message_id)
    await clear_old_messages(bot, chat_id, user_id)

    labs = get_user_labs(user_id, limit=8)

    if not labs:
        sent = await message.answer("📭 У вас пока нет сохраненных отчетов. Отправьте методичку для генерации первого!")
        track_message(user_id, sent.message_id)
        return

    text = "📚 **История ваших последних отчетов:**\n\n"
    buttons = []
    for idx, lab in enumerate(labs, 1):
        created = str(lab.get("created_at", ""))[:16]
        title = lab.get("title") or f"Лабораторная работа #{lab.get('id')}"
        text += f"**{idx}.** {title}\n📅 _{created}_\n\n"
        buttons.append([
            InlineKeyboardButton(text=f"📥 Скачать #{idx}", callback_data=f"getlab_{lab.get('id')}")
        ])

    buttons.append([InlineKeyboardButton(text="🔄 Закрыть", callback_data="close_labs")])
    sent = await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    track_message(user_id, sent.message_id)


@router.callback_query(F.data == "close_labs")
async def cb_close_labs(call: CallbackQuery):
    await safe_delete_msg(call.bot, call.message.chat.id, call.message.message_id)


@router.callback_query(F.data.startswith("getlab_"))
async def cb_download_lab(call: CallbackQuery):
    lab_id = int(call.data.split("_")[1])
    user_id = call.from_user.id
    labs = get_user_labs(user_id, limit=50)
    target = next((l for l in labs if l.get("id") == lab_id), None)

    if not target:
        await call.answer("Работа не найдена.", show_alert=True)
        return

    docx_path = target.get("docx_path")
    pdf_path = target.get("pdf_path")

    await call.answer("Отправляю файлы...")
    if docx_path and os.path.exists(docx_path):
        await call.message.answer_document(FSInputFile(docx_path, filename="Laboratornaya_Rabota.docx"))
    if pdf_path and os.path.exists(pdf_path):
        await call.message.answer_document(FSInputFile(pdf_path, filename="Laboratornaya_Rabota.pdf"))


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await safe_delete_msg(bot, chat_id, message.message_id)
    await clear_old_messages(bot, chat_id, user_id)

    if user_id in user_sessions:
        del user_sessions[user_id]
    user_dir = get_user_dir(user_id)
    if os.path.exists(user_dir):
        try:
            shutil.rmtree(user_dir)
        except Exception:
            pass

    sent = await message.answer("🧹 **Сессия сброшена.** Отправьте новый файл или фото задания.")
    track_message(user_id, sent.message_id)


# -------------------------------------------------------------
# Обработка фотографий (Multimodal OCR)
# -------------------------------------------------------------
@router.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await clear_old_messages(bot, chat_id, user_id)

    photo = message.photo[-1]
    user_dir = get_user_dir(user_id)
    local_photo_path = os.path.join(user_dir, f"task_photo_{photo.file_id}.jpg")

    status_msg = await message.answer("📸 Распознаю текст и задание на фото...")
    track_message(user_id, status_msg.message_id)

    try:
        await message.bot.download(photo, destination=local_photo_path)
        extracted_text = await extract_text_from_file(local_photo_path, max_chars=config.MAX_PROMPT_CHARS)

        if not extracted_text.strip() or len(extracted_text) < 20:
            await safe_edit(status_msg, "❌ Не удалось распознать текст на фотографии. Попробуйте сделать снимок четче.")
            return

        user_sessions[user_id] = {
            "lab_text": extracted_text,
            "session_dir": user_dir,
            "last_code": "",
            "last_docx": "",
            "waiting_input": None,
            "card_msg_id": status_msg.message_id,
            "clean_stack": [status_msg.message_id]
        }

        prof = get_profile(user_id)
        caption = message.caption or ""
        if caption.strip():
            user_sessions[user_id]["custom_instructions"] = caption

        preview_text = extracted_text[:300] + ("..." if len(extracted_text) > 300 else "")
        await safe_edit(
            status_msg,
            f"✅ **Задание распознано!**\n\n"
            f"📖 *Фрагмент:*\n_{preview_text}_\n\n"
            f"Нажмите **«Сгенерировать отчет»** для запуска:",
            reply_markup=get_options_keyboard(prof, user_sessions[user_id].get("custom_instructions", ""))
        )
    except Exception as e:
        logger.exception("Ошибка при обработке фото")
        await safe_edit(status_msg, f"❌ Ошибка распознавания фото:\n`{str(e)}`")


# -------------------------------------------------------------
# Обработка документов (.pdf, .docx, .txt, .md, картинки)
# -------------------------------------------------------------
@router.message(F.document)
async def handle_document(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await clear_old_messages(bot, chat_id, user_id)

    doc = message.document
    filename = doc.file_name or "uploaded_file"
    ext = os.path.splitext(filename)[1].lower()

    allowed = [".pdf", ".docx", ".txt", ".md", ".jpg", ".jpeg", ".png", ".webp"]
    if ext not in allowed:
        sent = await message.answer("⚠️ Поддерживаются форматы `.pdf`, `.docx`, `.txt`, `.md`, а также фото (`.jpg`, `.png`).")
        track_message(user_id, sent.message_id)
        return

    user_dir = get_user_dir(user_id)
    local_file_path = os.path.join(user_dir, filename)

    status_msg = await message.answer(f"📥 Читаю `{filename}`...")
    track_message(user_id, status_msg.message_id)

    try:
        await message.bot.download(doc, destination=local_file_path)
        extracted_text = await extract_text_from_file(local_file_path, max_chars=config.MAX_PROMPT_CHARS)

        if not extracted_text.strip():
            await safe_edit(status_msg, "❌ Не удалось извлечь текст. Возможно, файл пустой или поврежден.")
            return

        user_sessions[user_id] = {
            "lab_text": extracted_text,
            "session_dir": user_dir,
            "last_code": "",
            "last_docx": "",
            "waiting_input": None,
            "card_msg_id": status_msg.message_id,
            "clean_stack": [status_msg.message_id]
        }

        prof = get_profile(user_id)
        caption = message.caption or ""
        if caption.strip():
            user_sessions[user_id]["custom_instructions"] = caption

        preview_text = extracted_text[:300] + ("..." if len(extracted_text) > 300 else "")
        await safe_edit(
            status_msg,
            f"✅ **Методичка `{filename}` прочитана!**\n\n"
            f"📖 *Фрагмент задания:*\n_{preview_text}_\n\n"
            f"Нажмите **«Сгенерировать отчет»** для запуска:",
            reply_markup=get_options_keyboard(prof, user_sessions[user_id].get("custom_instructions", ""))
        )
    except Exception as e:
        logger.exception("Ошибка при обработке документа")
        await safe_edit(status_msg, f"❌ Ошибка при чтении файла:\n`{str(e)}`")


# -------------------------------------------------------------
# Обработка Inline-кнопок опций и профиля
# -------------------------------------------------------------
@router.callback_query(F.data.in_({
    "toggle_title", "toggle_quest", "toggle_theor", "toggle_chart", "toggle_anti", "toggle_anti_prof"
}))
async def cb_toggle_options(call: CallbackQuery):
    user_id = call.from_user.id
    prof = get_profile(user_id)

    if call.data == "toggle_title":
        update_profile(user_id, include_title=0 if prof.get("include_title", 1) else 1)
    elif call.data == "toggle_quest":
        update_profile(user_id, include_questions=0 if prof.get("include_questions", 1) else 1)
    elif call.data == "toggle_theor":
        update_profile(user_id, omit_theory=0 if prof.get("omit_theory", 1) else 1)
    elif call.data == "toggle_chart":
        update_profile(user_id, include_charts=0 if prof.get("include_charts", 1) else 1)
    elif call.data in ["toggle_anti", "toggle_anti_prof"]:
        update_profile(user_id, antiplagiat_mode=0 if prof.get("antiplagiat_mode", 1) else 1)

    updated_prof = get_profile(user_id)
    await call.answer("Настройка обновлена")
    try:
        if call.data == "toggle_anti_prof":
            await call.message.edit_reply_markup(reply_markup=get_profile_keyboard(updated_prof))
        else:
            custom = user_sessions.get(user_id, {}).get("custom_instructions", "")
            await call.message.edit_reply_markup(reply_markup=get_options_keyboard(updated_prof, custom))
    except Exception:
        pass


@router.callback_query(F.data.in_({"set_variant", "prof_variant"}))
async def cb_set_variant(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["waiting_input"] = "variant"
    user_sessions[user_id]["active_card_msg"] = call.message

    await call.answer()
    back_to = "options" if call.data == "set_variant" else "profile"
    await safe_edit(call.message, "🎯 **Напишите номер вашего варианта** (например: `7` или `14`):", reply_markup=get_cancel_keyboard(back_to))


@router.callback_query(F.data.startswith("prof_"))
async def cb_edit_profile_field(call: CallbackQuery):
    user_id = call.from_user.id
    field = call.data.replace("prof_", "")
    field_names = {
        "full_name": "ваше ФИО (например: *Иванов Иван Иванович*)",
        "group_name": "номер учебной группы (например: *ИВТ-41*)",
        "university": "название ВУЗа или кафедры (например: *РТУ МИРЭА*)",
        "teacher": "ФИО преподавателя (например: *доц. Дьячкова О.В.*)",
        "discipline": "название дисциплины (например: *Проектирование и дизайн ИС*)",
    }
    if field in field_names:
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]["waiting_input"] = field
        user_sessions[user_id]["active_card_msg"] = call.message

        await call.answer()
        await safe_edit(
            call.message, 
            f"✏️ **Введите {field_names[field]}:**",
            reply_markup=get_cancel_keyboard("profile")
        )


@router.callback_query(F.data.startswith("cancel_"))
async def cb_cancel_input(call: CallbackQuery):
    user_id = call.from_user.id
    back_to = call.data.replace("cancel_", "")
    if user_id in user_sessions:
        user_sessions[user_id]["waiting_input"] = None

    prof = get_profile(user_id)
    await call.answer("Отменено")

    if back_to == "profile":
        anti_str = "ВКЛ (высокая оригинальность)" if prof.get("antiplagiat_mode", 1) else "ВЫКЛ"
        text = (
            "👤 **Ваш студенческий профиль:**\n\n"
            f"• **ФИО студента:** {prof.get('full_name') or '*(не указано)*'}\n"
            f"• **Группа:** {prof.get('group_name') or '*(не указано)*'}\n"
            f"• **ВУЗ / Кафедра:** {prof.get('university') or '*(не указано)*'}\n"
            f"• **Преподаватель:** {prof.get('teacher') or '*(не указано)*'}\n"
            f"• **Дисциплина:** {prof.get('discipline') or '*(не указано)*'}\n"
            f"• **Вариант по умолчанию:** {prof.get('variant') or '*(любой)*'}\n"
            f"• **Режим Антиплагиат:** {anti_str}\n\n"
            "Нажмите кнопку ниже, чтобы изменить нужное поле:"
        )
        await safe_edit(call.message, text, reply_markup=get_profile_keyboard(prof))
    else:
        custom = user_sessions.get(user_id, {}).get("custom_instructions", "")
        await safe_edit(call.message, "✅ **Параметры работы:**\nНажмите **«Сгенерировать отчет»** для запуска:", reply_markup=get_options_keyboard(prof, custom))


@router.callback_query(F.data == "close_profile")
async def cb_close_profile(call: CallbackQuery):
    await call.answer("Профиль сохранен!")
    await safe_delete_msg(call.bot, call.message.chat.id, call.message.message_id)


@router.callback_query(F.data == "enter_custom_text")
async def cb_enter_custom(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["waiting_input"] = "custom_instructions"
    user_sessions[user_id]["active_card_msg"] = call.message

    await call.answer()
    await safe_edit(
        call.message, 
        "✍️ **Напишите ваши индивидуальные пожелания к отчету:**",
        reply_markup=get_cancel_keyboard("options")
    )


@router.callback_query(F.data == "start_generation")
async def cb_start_generation(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in user_sessions or not user_sessions[user_id].get("lab_text"):
        await call.answer("Сначала отправьте методичку или фото задания!", show_alert=True)
        return

    await call.answer("Генерация запущена!")
    status_msg = call.message
    await safe_edit(status_msg, "⚡ **Шаг 1/2:** Нейросеть решает задачи и формирует структуру работы...")
    custom = user_sessions[user_id].get("custom_instructions", "")
    await process_generation(call.message, status_msg, user_id, custom)


# -------------------------------------------------------------
# Обработка текстовых сообщений
# -------------------------------------------------------------
@router.message(F.text)
async def handle_text_message(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot
    user_text = message.text.strip()

    # Всегда удаляем входящее текстовое сообщение пользователя для чистоты чата
    await safe_delete_msg(bot, chat_id, message.message_id)

    # Проверка: ожидается ли ввод поля профиля или пожеланий
    if user_id in user_sessions and user_sessions[user_id].get("waiting_input"):
        field = user_sessions[user_id]["waiting_input"]
        user_sessions[user_id]["waiting_input"] = None
        card_msg = user_sessions[user_id].get("active_card_msg")

        if field == "custom_instructions":
            user_sessions[user_id]["custom_instructions"] = user_text
            prof = get_profile(user_id)
            if card_msg:
                await safe_edit(
                    card_msg, 
                    f"✅ **Пожелания сохранены:** _{user_text}_\n\nНажмите **«Сгенерировать отчет»** для запуска:",
                    reply_markup=get_options_keyboard(prof, user_text)
                )
            return
        elif field in ["full_name", "group_name", "university", "teacher", "discipline", "variant"]:
            update_profile(user_id, **{field: user_text})
            prof = get_profile(user_id)
            anti_str = "ВКЛ (высокая оригинальность)" if prof.get("antiplagiat_mode", 1) else "ВЫКЛ"
            text = (
                "👤 **Ваш студенческий профиль:**\n\n"
                f"• **ФИО студента:** {prof.get('full_name') or '*(не указано)*'}\n"
                f"• **Группа:** {prof.get('group_name') or '*(не указано)*'}\n"
                f"• **ВУЗ / Кафедра:** {prof.get('university') or '*(не указано)*'}\n"
                f"• **Преподаватель:** {prof.get('teacher') or '*(не указано)*'}\n"
                f"• **Дисциплина:** {prof.get('discipline') or '*(не указано)*'}\n"
                f"• **Вариант по умолчанию:** {prof.get('variant') or '*(любой)*'}\n"
                f"• **Режим Антиплагиат:** {anti_str}\n\n"
                "Нажмите кнопку ниже, чтобы изменить нужное поле:"
            )
            if card_msg:
                await safe_edit(card_msg, text, reply_markup=get_profile_keyboard(prof))
            return

    if user_id not in user_sessions or not user_sessions[user_id].get("lab_text"):
        sent = await message.answer("ℹ️ Отправьте файл методички (`.pdf`, `.docx`) или фото задания для начала работы.")
        track_message(user_id, sent.message_id)
        return

    session = user_sessions[user_id]
    has_previous_code = bool(session.get("last_code"))

    if not has_previous_code:
        session["custom_instructions"] = user_text
        status_msg = await message.answer("⚡ **Шаг 1/2:** Нейросеть решает задачи и формирует структуру работы...")
        track_message(user_id, status_msg.message_id)
        await process_generation(message, status_msg, user_id, user_text)
    else:
        status_msg = await message.answer("🔄 **Вношу правки в отчет...**")
        track_message(user_id, status_msg.message_id)
        await process_edit(message, status_msg, user_id, user_text)


async def process_generation(message: Message, status_msg: Message, user_id: int, instructions: str):
    session = user_sessions[user_id]
    lab_text = session["lab_text"]
    user_dir = session["session_dir"]
    prof = get_profile(user_id)

    try:
        code = await llm_service.generate_initial_code(lab_text, instructions, profile=prof)

        await safe_edit(status_msg, "⚡ **Шаг 2/2:** Компилирую документ Word, строю графики и собираю PDF...")
        success, docx_path, pdf_path, chart_paths, error_msg = await execute_with_auto_fix(code, user_dir, status_msg)

        if success and docx_path and os.path.exists(docx_path):
            session["last_code"] = code
            session["last_docx"] = docx_path
            
            disc = prof.get("discipline") or "Лабораторная работа"
            add_lab_history(user_id, title=disc, discipline=disc, docx_path=docx_path, pdf_path=pdf_path)

            await send_completed_file(message, status_msg, docx_path, pdf_path, chart_paths)
        else:
            await safe_edit(
                status_msg,
                f"❌ Не удалось скомпилировать документ:\n\n`{error_msg[:800] if error_msg else 'Ошибка исполнения'}`\n\n"
                "Попробуйте отправить /reset или уточнить задание."
            )
    except Exception as e:
        logger.exception("Ошибка в процессе генерации")
        await safe_edit(status_msg, f"❌ Ошибка генерации:\n`{str(e)}`")


async def process_edit(message: Message, status_msg: Message, user_id: int, edit_instructions: str):
    session = user_sessions[user_id]
    old_code = session["last_code"]
    user_dir = session["session_dir"]
    prof = get_profile(user_id)

    try:
        new_code = await llm_service.edit_existing_code(old_code, edit_instructions)
        await safe_edit(status_msg, "⚡ Пересобираю документ Word и обновляю PDF...")
        success, docx_path, pdf_path, chart_paths, error_msg = await execute_with_auto_fix(new_code, user_dir, status_msg)

        if success and docx_path and os.path.exists(docx_path):
            session["last_code"] = new_code
            session["last_docx"] = docx_path
            
            disc = prof.get("discipline") or "Лабораторная работа"
            add_lab_history(user_id, title=f"{disc} (с правками)", discipline=disc, docx_path=docx_path, pdf_path=pdf_path)

            await send_completed_file(message, status_msg, docx_path, pdf_path, chart_paths, is_update=True)
        else:
            await safe_edit(status_msg, f"❌ Ошибка сборки с правками:\n\n`{error_msg[:800] if error_msg else 'Ошибка'}`")
    except Exception as e:
        logger.exception("Ошибка при редактировании документа")
        await safe_edit(status_msg, f"❌ Ошибка при внесении правок:\n`{str(e)}`")


async def execute_with_auto_fix(code: str, user_dir: str, status_msg: Message, max_attempts: int = 3):
    current_code = code

    for attempt in range(1, max_attempts + 1):
        success, docx_path, pdf_path, chart_paths, error_msg = await run_generated_script(
            current_code,
            user_dir,
            timeout_seconds=config.RUNNER_TIMEOUT_SECONDS
        )

        if success:
            return True, docx_path, pdf_path, chart_paths, None

        if attempt < max_attempts:
            await safe_edit(status_msg, f"🔧 Авто-исправление кода (попытка {attempt+1}/{max_attempts})...")
            current_code = await llm_service.fix_broken_code(current_code, error_msg or "Unknown error")

    return False, None, None, [], error_msg


async def send_completed_file(
    message: Message, 
    status_msg: Message, 
    docx_path: str, 
    pdf_path: Optional[str] = None, 
    chart_paths: Optional[list] = None,
    is_update: bool = False
):
    # Удаляем служебное статусное сообщение, чтобы не засорять чат
    try:
        await status_msg.delete()
    except Exception:
        pass

    # 1. Отправка превью графиков
    if chart_paths:
        for chart_file in chart_paths[:3]:
            try:
                await message.answer_photo(
                    photo=FSInputFile(chart_file),
                    caption="📊 **График/схема к лабораторной работе**"
                )
            except Exception:
                pass

    # 2. Отправка DOCX
    action_word = "обновлен" if is_update else "готов"
    caption_docx = (
        f"✅ **Ваш отчет успешно {action_word}!**\n\n"
        "📎 **Формат:** Microsoft Word (`.docx`)\n"
        "📐 **Оформление:** ГОСТ (поля 30/15/20/20, Times New Roman, таблицы, формулы OMML)\n\n"
        "💬 **Нужны правки?** Напишите замечания в чат (например: *«исправь формулу 2»*, *«убери лишнюю теорию»*)."
    )
    await message.answer_document(
        document=FSInputFile(docx_path, filename="Laboratornaya_Rabota.docx"),
        caption=caption_docx,
        parse_mode="Markdown"
    )

    # 3. Отправка PDF (если сконвертирован)
    if pdf_path and os.path.exists(pdf_path):
        caption_pdf = "📑 **Версия для быстрой сдачи и просмотра (PDF)**"
        await message.answer_document(
            document=FSInputFile(pdf_path, filename="Laboratornaya_Rabota.pdf"),
            caption=caption_pdf,
            parse_mode="Markdown"
        )
