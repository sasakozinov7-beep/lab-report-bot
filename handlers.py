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
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
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
    get_user_labs,
    get_last_lab,
    STYLE_PRESETS
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
# Постоянное нижнее меню (Reply Keyboard)
# -------------------------------------------------------------
def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянные кнопки внизу экрана для максимального удобства."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📁 Сдать новую работу"), 
                KeyboardButton(text="👤 Мой профиль (МТИ)")
            ],
            [
                KeyboardButton(text="🧠 Память стиля"), 
                KeyboardButton(text="📚 Мои отчеты")
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


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
# Клавиатуры и тексты интерфейса
# -------------------------------------------------------------
def get_options_keyboard(profile: Dict[str, Any], custom_text: str = "", sample_name: str = "") -> InlineKeyboardMarkup:
    """Интерактивная клавиатура параметров отчета с наглядными и понятными кнопками."""
    t_title = "✅ МТИ" if profile.get("include_title", 1) else "❌ Выкл"
    t_quest = "✅ Ответы" if profile.get("include_questions", 1) else "❌ Выкл"
    # omit_theory: 1 = скрыть теорию, 0 = включить теорию
    t_theor = "❌ Скрыть" if profile.get("omit_theory", 1) else "✅ Включить"
    t_chart = "✅ Строить" if profile.get("include_charts", 1) else "❌ Выкл"
    t_anti = "✅ 85%+" if profile.get("antiplagiat_mode", 1) else "⚡ Обычный"
    variant = profile.get("variant")
    t_var = f"№ {variant}" if variant else "🎲 Любой"

    preset_key = profile.get("active_style_preset", "standard")
    preset_info = STYLE_PRESETS.get(preset_key, STYLE_PRESETS["standard"])
    format_label = f"🎨 Формат: {preset_info['short']}"

    if sample_name:
        custom_label = f"📎 Образец: {sample_name[:14]}..."
    elif custom_text:
        custom_label = f"💬 Пожелания: {custom_text[:14]}..."
    else:
        custom_label = "✏️ Написать пожелания текстом"

    rows = [
        [
            InlineKeyboardButton(text=f"🎓 Титульник: {t_title}", callback_data="toggle_title"),
            InlineKeyboardButton(text=f"❓ Вопросы: {t_quest}", callback_data="toggle_quest")
        ],
        [
            InlineKeyboardButton(text=f"📖 Теория: {t_theor}", callback_data="toggle_theor"),
            InlineKeyboardButton(text=f"📊 Графики: {t_chart}", callback_data="toggle_chart")
        ],
        [
            InlineKeyboardButton(text=f"🛡️ Антиплагиат: {t_anti}", callback_data="toggle_anti"),
            InlineKeyboardButton(text=f"🎯 Вариант: {t_var}", callback_data="set_variant")
        ],
        [
            InlineKeyboardButton(text=format_label, callback_data="open_style_menu")
        ],
        [
            InlineKeyboardButton(text=custom_label, callback_data="enter_custom_text")
        ]
    ]

    if custom_text or sample_name:
        rows.append([
            InlineKeyboardButton(text="🗑️ Сбросить пожелания", callback_data="clear_custom_text")
        ])

    rows.append([
        InlineKeyboardButton(text="🚀 Сгенерировать отчет", callback_data="start_generation")
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_profile_keyboard(profile: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Клавиатура редактирования профиля."""
    anti = "ВКЛ (85%+)" if profile.get("antiplagiat_mode", 1) else "ВЫКЛ"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ ФИО Студента", callback_data="prof_full_name"),
                InlineKeyboardButton(text="✏️ Учебная группа", callback_data="prof_group_name")
            ],
            [
                InlineKeyboardButton(text="✏️ Преподаватель", callback_data="prof_teacher"),
                InlineKeyboardButton(text="✏️ Дисциплина", callback_data="prof_discipline")
            ],
            [
                InlineKeyboardButton(text="🏛️ ВУЗ / Кафедра", callback_data="prof_university"),
                InlineKeyboardButton(text="🎯 Вариант", callback_data="prof_variant")
            ],
            [
                InlineKeyboardButton(text="📋 Настроить титульник (МТИ)", callback_data="prof_title_template")
            ],
            [
                InlineKeyboardButton(text="🧠 Память стиля и пресеты", callback_data="open_style_menu")
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


def get_style_keyboard(current_preset: str = "standard") -> InlineKeyboardMarkup:
    """Клавиатура выбора стиля и формата работы."""
    def mark(key: str, name: str) -> str:
        return f"🟢 {name}" if key == current_preset else name

    rows = [
        [
            InlineKeyboardButton(text=mark("standard", "📝 Полный ГОСТ"), callback_data="style_set_standard"),
            InlineKeyboardButton(text=mark("qa_only", "⚡ Вопрос-ответ"), callback_data="style_set_qa_only")
        ],
        [
            InlineKeyboardButton(text=mark("practice_only", "🔬 Только практика"), callback_data="style_set_practice_only"),
            InlineKeyboardButton(text=mark("repeat_last", "🔄 Как прошлая"), callback_data="style_set_repeat_last")
        ],
        [
            InlineKeyboardButton(text="✏️ Написать свои требования текстом", callback_data="style_enter_custom")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад к параметрам", callback_data="style_back_to_options")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_style_menu_text(profile: Dict[str, Any]) -> str:
    """Текст карточки настройки памяти стиля и форматов."""
    preset_key = profile.get("active_style_preset", "standard")
    preset_info = STYLE_PRESETS.get(preset_key, STYLE_PRESETS["standard"])
    saved_inst = profile.get("last_custom_instructions", "")

    inst_block = f"\n💬 *Сохраненные указания:* «_{saved_inst}_»\n" if saved_inst else ""

    return (
        "🧠 **Память стиля и формата отчетов:**\n\n"
        f"📌 **Текущий режим:** **{preset_info['title']}**\n"
        f"_{preset_info['desc']}_{inst_block}\n\n"
        "⚡ **Выберите формат работы в 1 клик:**\n"
        "• **📝 Полный ГОСТ** — классический отчет: титульник, введение, ход работы, таблицы, графики, заключение, вопросы.\n"
        "• **⚡ Только вопросы и ответы** — без введения и заключения! Строго четкий вопрос и подробный аргументированный ответ.\n"
        "• **🔬 Только практика** — без лишней теории, сразу к расчетам, формулам и вычислениям.\n"
        "• **🔄 Как в прошлый раз** — повторить структуру и требования предыдущей выполненной работы.\n\n"
        "💡 *Вы также можете в любой момент написать в чат, например: «сделай так же, как предыдущую, без введения и заключения».*"
    )


def get_cancel_keyboard(back_to: str = "profile") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Отмена", callback_data=f"cancel_{back_to}")
        ]]
    )


# -------------------------------------------------------------
# Команды и главное меню
# -------------------------------------------------------------
@router.message(CommandStart())
@router.message(F.text == "📁 Сдать новую работу")
async def cmd_start(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await safe_delete_msg(bot, chat_id, message.message_id)

    prof = get_profile(user_id)
    preset_key = prof.get("active_style_preset", "standard")
    preset_info = STYLE_PRESETS.get(preset_key, STYLE_PRESETS["standard"])
    student_str = f"{prof.get('full_name')} ({prof.get('group_name')})"

    welcome_text = (
        "🎓 **Привет! Я твой академический ассистент по лабораторным работам.**\n\n"
        "Я собираю полноценные отчеты по ГОСТу — с формулами, расчетами, таблицами, графиками и правильным титульником.\n\n"
        "✨ **Текущие настройки по умолчанию:**\n"
        f"• 🏛️ **Титульный лист:** МТИ ({student_str})\n"
        f"• 🎨 **Формат отчета:** {preset_info['title']}\n"
        "• 📐 **Оформление:** ГОСТ (Times New Roman, поля 30/15/20/20, строгий черный текст)\n\n"
        "🚀 **Как получить готовый отчет:**\n"
        "1. Отправь **файл методички** (`.docx`, `.pdf`, `.txt`) или **фото задания** сюда в чат.\n"
        "2. Или напиши свои пожелания (например: *«сделай так же, как предыдущую, без введения и заключения»*).\n"
        "3. Забирай готовый документ в **DOCX** и **PDF**!"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Профиль и титульник (МТИ)", callback_data="open_profile"),
                InlineKeyboardButton(text="🧠 Память стиля", callback_data="open_style_menu")
            ],
            [
                InlineKeyboardButton(text="📚 Мои отчеты", callback_data="open_my_labs"),
                InlineKeyboardButton(text="❓ Как пользоваться", callback_data="open_help")
            ]
        ]
    )

    sent = await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")
    track_message(user_id, sent.message_id)

    # Прикрепляем постоянную нижнюю клавиатуру Telegram
    try:
        await message.answer("👇 Меню быстрого доступа:", reply_markup=get_main_reply_keyboard())
    except Exception:
        pass


@router.message(Command("profile"))
@router.message(F.text == "👤 Мой профиль (МТИ)")
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await safe_delete_msg(bot, chat_id, message.message_id)

    prof = get_profile(user_id)
    anti_str = "ВКЛ (высокая оригинальность 85%+)" if prof.get("antiplagiat_mode", 1) else "ВЫКЛ (базовый)"
    preset_key = prof.get("active_style_preset", "standard")
    preset_info = STYLE_PRESETS.get(preset_key, STYLE_PRESETS["standard"])

    text = (
        "👤 **Ваш студенческий профиль:**\n\n"
        f"• **ФИО студента:** {prof.get('full_name') or '*(не указано)*'}\n"
        f"• **Учебная группа:** {prof.get('group_name') or '*(не указано)*'}\n"
        f"• **ВУЗ / Организация:** {prof.get('university') or '*(не указано)*'}\n"
        f"• **Преподаватель:** {prof.get('teacher') or '*(не указано)*'}\n"
        f"• **Дисциплина:** {prof.get('discipline') or '*(не указано)*'}\n"
        f"• **Вариант по умолчанию:** {prof.get('variant') or '*(любой)*'}\n\n"
        "🏛️ **Оформление титульного листа:**\n"
        "• **Стандарт:** МОСКОВСКИЙ ТЕХНОЛОГИЧЕСКИЙ ИНСТИТУТ (МТИ) ✅\n"
        "• *Титульник создается автоматически по зафиксированному образцу.*\n\n"
        f"🎨 **Стиль оформления:** {preset_info['title']}\n"
        f"🛡️ **Антиплагиат:** {anti_str}\n\n"
        "👇 *Нажмите кнопку ниже, чтобы изменить нужное поле:*"
    )
    sent = await message.answer(text, reply_markup=get_profile_keyboard(prof), parse_mode="Markdown")
    track_message(user_id, sent.message_id)


@router.message(Command("style"))
@router.message(F.text == "🧠 Память стиля")
async def cmd_style(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await safe_delete_msg(bot, chat_id, message.message_id)

    prof = get_profile(user_id)
    text = get_style_menu_text(prof)
    kb = get_style_keyboard(prof.get("active_style_preset", "standard"))
    sent = await message.answer(text, reply_markup=kb, parse_mode="Markdown")
    track_message(user_id, sent.message_id)


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    await safe_delete_msg(bot, chat_id, message.message_id)

    text = (
        "📖 **Инструкция по использованию бота:**\n\n"
        "1️⃣ **Отправка задания:**\n"
        "Прикрепите методичку (`.docx`, `.pdf`, `.txt`) или фото страниц задания прямо в чат.\n\n"
        "2️⃣ **Память стиля и пожелания:**\n"
        "Вы можете в любой момент написать: *«сделай так же, как предыдущую»* или "
        "*«без введения и заключения, только вопросы и ответы»* — бот запомнит это и применит к отчету.\n\n"
        "3️⃣ **Титульный лист:**\n"
        "По умолчанию установлен стандарт МТИ с вашими данными. Если титульник не нужен, просто нажмите кнопку `🎓 Титульник: Выкл`.\n\n"
        "4️⃣ **Правки:**\n"
        "После получения отчета напишите замечания в чат (например: *«замени преподавателя»*, *«убери график»*), и бот мгновенно пересоберет документ."
    )
    sent = await message.answer(text, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Понятно", callback_data="close_profile")]]
    ), parse_mode="Markdown")
    track_message(user_id, sent.message_id)


@router.message(Command("my_labs"))
@router.message(F.text == "📚 Мои отчеты")
async def cmd_my_labs(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot

    # Удаляем команду /my_labs из чата
    await safe_delete_msg(bot, chat_id, message.message_id)

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
    await call.answer()
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

    # Удаляем команду /reset из чата
    await safe_delete_msg(bot, chat_id, message.message_id)

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
# Вспомогательное прикрепление образца титульника/оформления
# -------------------------------------------------------------
async def attach_sample_to_session(
    user_id: int, 
    chat_id: int, 
    bot, 
    sample_text: str, 
    sample_name: str, 
    caption: str, 
    status_msg: Message,
    message: Message,
    is_edit_mode: bool = False
):
    session = user_sessions.setdefault(user_id, {})
    sample_block = (
        f"📋 ОБРАЗЕЦ ТИТУЛЬНОГО ЛИСТА / ОФОРМЛЕНИЯ (из файла «{sample_name}»):\n"
        "В точности скопируй структуру, шапку министерства/ВУЗа, кафедры, формулировки блоков Выполнил/Проверил, город и год:\n"
        f"{sample_text.strip()}"
    )
    if caption:
        instruction = f"{caption}\n\n{sample_block}"
    else:
        instruction = sample_block

    if is_edit_mode:
        session["waiting_input"] = None
        await safe_edit(status_msg, f"🔄 **Применяю образец титульника `{sample_name}` к текущему отчету...**\n⚡ Пересобираю документ Word и PDF...")
        await process_edit(message, status_msg, user_id, instruction)
        return

    # До генерации: сохраняем образец в параметры отчета
    existing = session.get("custom_instructions", "")
    if existing and sample_block not in existing:
        session["custom_instructions"] = f"{existing}\n\n{instruction}"
    else:
        session["custom_instructions"] = instruction

    session["sample_filename"] = sample_name
    session["waiting_input"] = None

    card_msg = session.get("active_card_msg")
    prof = get_profile(user_id)
    preview = sample_text.strip()[:250] + ("..." if len(sample_text.strip()) > 250 else "")

    card_text = (
        f"✅ **Образец оформления прикреплен к работе!**\n\n"
        f"📎 **Файл-образец:** `{sample_name}`\n"
        f"{f'💬 *Пожелание:* _{caption}_' + chr(10) + chr(10) if caption else ''}"
        f"📖 *Распознано из образца:*\n_{preview}_\n\n"
        f"🎓 Нейросеть воспроизведет структуру и шапку титульника в точности по этому образцу.\n"
        f"Нажмите **«Сгенерировать отчет»** для запуска:"
    )

    kb = get_options_keyboard(prof, session["custom_instructions"], sample_name=sample_name)
    if card_msg and card_msg.message_id != status_msg.message_id:
        await safe_edit(card_msg, card_text, reply_markup=kb)
        await safe_delete_msg(bot, chat_id, status_msg.message_id)
    else:
        await safe_edit(status_msg, card_text, reply_markup=kb)
        session["active_card_msg"] = status_msg

    if prof.get("clean_chat_mode", 0) == 1:
        await safe_delete_msg(bot, chat_id, message.message_id)


# -------------------------------------------------------------
# Обработка фотографий (Multimodal OCR)
# -------------------------------------------------------------
@router.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot = message.bot
    prof = get_profile(user_id)

    session = user_sessions.setdefault(user_id, {})
    waiting_input = session.get("waiting_input")
    caption = (message.caption or "").strip()
    caption_lower = caption.lower()
    is_sample_caption = any(k in caption_lower for k in ["титульн", "образец", "пример", "шаблон", "оформлен", "как тут", "как здесь"])

    is_wish_or_sample = (
        waiting_input in ["custom_instructions", "sample_file", "edit_sample"] or 
        (bool(session.get("lab_text")) and is_sample_caption)
    )

    photo = message.photo[-1]
    user_dir = get_user_dir(user_id)
    local_photo_path = os.path.join(user_dir, f"photo_{photo.file_id}.jpg")

    if waiting_input == "custom_title_template":
        status_msg = await message.answer("📸 Считываю постоянный шаблон титульника...")
        track_message(user_id, status_msg.message_id)
        try:
            await message.bot.download(photo, destination=local_photo_path)
            sample_text = await llm_service.extract_sample_title_page(local_photo_path)
            if not sample_text.strip():
                await safe_edit(status_msg, "❌ Не удалось распознать текст шаблона на фото.")
                return
            update_profile(user_id, custom_title_template=sample_text.strip())
            session["waiting_input"] = None
            updated_prof = get_profile(user_id)
            anti_str = "ВКЛ (высокая оригинальность)" if updated_prof.get("antiplagiat_mode", 1) else "ВЫКЛ"
            text = (
                "👤 **Ваш студенческий профиль:**\n\n"
                f"• **ФИО студента:** {updated_prof.get('full_name') or '*(не указано)*'}\n"
                f"• **Группа:** {updated_prof.get('group_name') or '*(не указано)*'}\n"
                f"• **ВУЗ / Организация:** {updated_prof.get('university') or '*(не указано)*'}\n"
                f"• **Преподаватель:** {updated_prof.get('teacher') or '*(не указано)*'}\n"
                f"• **Дисциплина:** {updated_prof.get('discipline') or '*(не указано)*'}\n"
                f"• **Вариант по умолчанию:** {updated_prof.get('variant') or '*(любой)*'}\n"
                f"• **Шаблон титульника:** Сохранен с фото\n"
                f"• **Режим Антиплагиат:** {anti_str}\n\n"
                "✅ *Новый постоянный образец титульника сохранен!*\n"
                "Нажмите кнопку ниже, чтобы изменить нужное поле:"
            )
            card_msg = session.get("active_card_msg")
            if card_msg:
                await safe_edit(card_msg, text, reply_markup=get_profile_keyboard(updated_prof))
                await safe_delete_msg(bot, chat_id, status_msg.message_id)
            else:
                await safe_edit(status_msg, text, reply_markup=get_profile_keyboard(updated_prof))
            if prof.get("clean_chat_mode", 0) == 1:
                await safe_delete_msg(bot, chat_id, message.message_id)
        except Exception as e:
            await safe_edit(status_msg, f"❌ Ошибка сохранения шаблона: {e}")
        return

    if is_wish_or_sample:
        status_msg = await message.answer("📸 Изучаю образец оформления на фотографии...")
        track_message(user_id, status_msg.message_id)
        try:
            await message.bot.download(photo, destination=local_photo_path)
            sample_text = await llm_service.extract_sample_title_page(local_photo_path)
            if not sample_text.strip():
                await safe_edit(status_msg, "❌ Не удалось распознать образец на фотографии. Попробуйте сделать снимок четче.")
                return

            is_edit_mode = bool(session.get("last_code")) or (waiting_input == "edit_sample")
            await attach_sample_to_session(
                user_id=user_id,
                chat_id=chat_id,
                bot=bot,
                sample_text=sample_text,
                sample_name="Фото титульника",
                caption=caption,
                status_msg=status_msg,
                message=message,
                is_edit_mode=is_edit_mode
            )
        except Exception as e:
            logger.exception("Ошибка при обработке фото образца")
            await safe_edit(status_msg, f"❌ Ошибка распознавания образца:\n`{str(e)}`")
        return

    # Обычная загрузка фото задания
    if prof.get("clean_chat_mode", 0) == 1:
        await clear_old_messages(bot, chat_id, user_id)

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
            "active_card_msg": status_msg,
            "clean_stack": [status_msg.message_id]
        }

        if caption:
            user_sessions[user_id]["custom_instructions"] = caption
        elif prof.get("last_custom_instructions"):
            user_sessions[user_id]["custom_instructions"] = prof.get("last_custom_instructions")

        custom_active = user_sessions[user_id].get("custom_instructions", "")
        preset_info = STYLE_PRESETS.get(prof.get("active_style_preset", "standard"), STYLE_PRESETS["standard"])
        preview_text = extracted_text[:280] + ("..." if len(extracted_text) > 280 else "")

        student_str = f"{prof.get('full_name')} ({prof.get('group_name')})"
        card_text = (
            f"📸 **Задание с фото распознано!**\n\n"
            f"👥 **Студент:** {student_str}\n"
            f"🏛️ **Титульный лист:** МТИ (преп. {prof.get('teacher')})\n"
            f"🎨 **Формат отчета:** {preset_info['title']}\n"
            f"{f'💬 *Пожелания:* _{custom_active}_' + chr(10) if custom_active else ''}\n"
            f"📖 *Фрагмент задания:*\n_{preview_text}_\n\n"
            f"Нажмите **«Сгенерировать отчет»** для запуска:"
        )
        await safe_edit(
            status_msg,
            card_text,
            reply_markup=get_options_keyboard(prof, custom_active)
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
    prof = get_profile(user_id)

    doc = message.document
    filename = doc.file_name or "uploaded_file"
    ext = os.path.splitext(filename)[1].lower()

    allowed = [".pdf", ".docx", ".txt", ".md", ".jpg", ".jpeg", ".png", ".webp"]
    if ext not in allowed:
        sent = await message.answer("⚠️ Поддерживаются форматы `.pdf`, `.docx`, `.txt`, `.md`, а также фото (`.jpg`, `.png`).")
        track_message(user_id, sent.message_id)
        return

    session = user_sessions.setdefault(user_id, {})
    waiting_input = session.get("waiting_input")
    caption = (message.caption or "").strip()
    caption_lower = caption.lower()
    is_sample_caption = any(k in caption_lower for k in ["титульн", "образец", "пример", "шаблон", "оформлен", "как тут", "как здесь"])

    is_wish_or_sample = (
        waiting_input in ["custom_instructions", "sample_file", "edit_sample"] or 
        (bool(session.get("lab_text")) and is_sample_caption)
    )

    user_dir = get_user_dir(user_id)
    local_file_path = os.path.join(user_dir, filename)

    if waiting_input == "custom_title_template":
        status_msg = await message.answer(f"📥 Считываю постоянный шаблон титульника из `{filename}`...")
        track_message(user_id, status_msg.message_id)
        try:
            await message.bot.download(doc, destination=local_file_path)
            sample_text = await extract_text_from_file(local_file_path, max_chars=config.MAX_PROMPT_CHARS)
            if not sample_text.strip():
                await safe_edit(status_msg, "❌ Не удалось прочитать шаблон из файла.")
                return
            update_profile(user_id, custom_title_template=sample_text.strip())
            session["waiting_input"] = None
            updated_prof = get_profile(user_id)
            anti_str = "ВКЛ (высокая оригинальность)" if updated_prof.get("antiplagiat_mode", 1) else "ВЫКЛ"
            text = (
                "👤 **Ваш студенческий профиль:**\n\n"
                f"• **ФИО студента:** {updated_prof.get('full_name') or '*(не указано)*'}\n"
                f"• **Группа:** {updated_prof.get('group_name') or '*(не указано)*'}\n"
                f"• **ВУЗ / Организация:** {updated_prof.get('university') or '*(не указано)*'}\n"
                f"• **Преподаватель:** {updated_prof.get('teacher') or '*(не указано)*'}\n"
                f"• **Дисциплина:** {updated_prof.get('discipline') or '*(не указано)*'}\n"
                f"• **Вариант по умолчанию:** {updated_prof.get('variant') or '*(любой)*'}\n"
                f"• **Шаблон титульника:** Сохранен из `{filename}`\n"
                f"• **Режим Антиплагиат:** {anti_str}\n\n"
                f"✅ *Постоянный образец титульника сохранен из `{filename}`!*\n"
                "Нажмите кнопку ниже, чтобы изменить нужное поле:"
            )
            card_msg = session.get("active_card_msg")
            if card_msg:
                await safe_edit(card_msg, text, reply_markup=get_profile_keyboard(updated_prof))
                await safe_delete_msg(bot, chat_id, status_msg.message_id)
            else:
                await safe_edit(status_msg, text, reply_markup=get_profile_keyboard(updated_prof))
            if prof.get("clean_chat_mode", 0) == 1:
                await safe_delete_msg(bot, chat_id, message.message_id)
        except Exception as e:
            await safe_edit(status_msg, f"❌ Ошибка сохранения шаблона: {e}")
        return

    if is_wish_or_sample:
        status_msg = await message.answer(f"📥 Изучаю образец оформления из `{filename}`...")
        track_message(user_id, status_msg.message_id)

        async def update_status_callback(text_update: str):
            await safe_edit(status_msg, text_update)

        try:
            await message.bot.download(doc, destination=local_file_path)
            sample_text = await extract_text_from_file(
                local_file_path, 
                max_chars=config.MAX_PROMPT_CHARS,
                status_callback=update_status_callback
            )
            if not sample_text.strip():
                await safe_edit(status_msg, "❌ Не удалось прочитать образец. Возможно, файл пустой.")
                return

            is_edit_mode = bool(session.get("last_code")) or (waiting_input == "edit_sample")
            await attach_sample_to_session(
                user_id=user_id,
                chat_id=chat_id,
                bot=bot,
                sample_text=sample_text,
                sample_name=filename,
                caption=caption,
                status_msg=status_msg,
                message=message,
                is_edit_mode=is_edit_mode
            )
        except Exception as e:
            logger.exception("Ошибка при чтении образца")
            await safe_edit(status_msg, f"❌ Ошибка при чтении образца:\n`{str(e)}`")
        return

    # Если уже есть сгенерированный отчет и файл прислан без ключевых слов — спрашиваем действие
    if session.get("last_code"):
        await message.bot.download(doc, destination=local_file_path)
        session["pending_new_doc"] = {
            "path": local_file_path,
            "filename": filename,
            "caption": caption
        }
        confirm_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Обновить текущий отчет по этому файлу", callback_data="choice_use_as_sample"),
                ],
                [
                    InlineKeyboardButton(text="🆕 Сделать новый отчет с нуля", callback_data="choice_start_new_lab")
                ],
                [
                    InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_file_choice")
                ]
            ]
        )
        sent = await message.answer(
            f"📄 **Вы отправили файл `{filename}`.**\n\n"
            "У вас уже есть сгенерированный отчет. Выберите действие:",
            reply_markup=confirm_kb
        )
        track_message(user_id, sent.message_id)
        return

    # Обычный сценарий: чтение методички для новой работы
    if prof.get("clean_chat_mode", 0) == 1:
        await clear_old_messages(bot, chat_id, user_id)

    status_msg = await message.answer(f"📥 Читаю `{filename}`...")
    track_message(user_id, status_msg.message_id)

    async def update_status_callback(text_update: str):
        await safe_edit(status_msg, text_update)

    try:
        await message.bot.download(doc, destination=local_file_path)
        extracted_text = await extract_text_from_file(
            local_file_path, 
            max_chars=config.MAX_PROMPT_CHARS,
            status_callback=update_status_callback
        )

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
            "active_card_msg": status_msg,
            "clean_stack": [status_msg.message_id]
        }

        if caption:
            user_sessions[user_id]["custom_instructions"] = caption
        elif prof.get("last_custom_instructions"):
            user_sessions[user_id]["custom_instructions"] = prof.get("last_custom_instructions")

        custom_active = user_sessions[user_id].get("custom_instructions", "")
        preset_info = STYLE_PRESETS.get(prof.get("active_style_preset", "standard"), STYLE_PRESETS["standard"])
        preview_text = extracted_text[:280] + ("..." if len(extracted_text) > 280 else "")

        student_str = f"{prof.get('full_name')} ({prof.get('group_name')})"
        card_text = (
            f"📄 **Методичка `{filename}` прочитана!**\n\n"
            f"👥 **Студент:** {student_str}\n"
            f"🏛️ **Титульный лист:** МТИ (преп. {prof.get('teacher')})\n"
            f"🎨 **Формат отчета:** {preset_info['title']}\n"
            f"{f'💬 *Пожелания:* _{custom_active}_' + chr(10) if custom_active else ''}\n"
            f"📖 *Фрагмент задания:*\n_{preview_text}_\n\n"
            f"Нажмите **«Сгенерировать отчет»** для запуска:"
        )
        await safe_edit(
            status_msg,
            card_text,
            reply_markup=get_options_keyboard(prof, custom_active)
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
            anti_str = "ВКЛ (высокая оригинальность)" if updated_prof.get("antiplagiat_mode", 1) else "ВЫКЛ"
            text = (
                "👤 **Ваш студенческий профиль:**\n\n"
                f"• **ФИО студента:** {updated_prof.get('full_name') or '*(не указано)*'}\n"
                f"• **Группа:** {updated_prof.get('group_name') or '*(не указано)*'}\n"
                f"• **ВУЗ / Кафедра:** {updated_prof.get('university') or '*(не указано)*'}\n"
                f"• **Преподаватель:** {updated_prof.get('teacher') or '*(не указано)*'}\n"
                f"• **Дисциплина:** {updated_prof.get('discipline') or '*(не указано)*'}\n"
                f"• **Вариант по умолчанию:** {updated_prof.get('variant') or '*(любой)*'}\n"
                f"• **Режим Антиплагиат:** {anti_str}\n\n"
                "💡 *Эти данные автоматически подставляются в титульный лист.*\n"
                "Нажмите кнопку ниже, чтобы изменить нужное поле:"
            )
            await safe_edit(call.message, text, reply_markup=get_profile_keyboard(updated_prof))
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


@router.callback_query(F.data == "prof_title_template")
async def cb_prof_title_template(call: CallbackQuery):
    user_id = call.from_user.id
    prof = get_profile(user_id)
    template = prof.get("custom_title_template") or "Образец МТИ"
    preview = template[:400] + ("..." if len(template) > 400 else "")

    text = (
        "📋 **Постоянный образец титульного листа:**\n\n"
        f"```\n{preview}\n```\n\n"
        "💡 *Этот титульник используется во всех отчетах по умолчанию. "
        "В нем автоматически обновляются преподаватель, студент, дисциплина и тема работы.*\n\n"
        "Что вы хотите сделать?"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Задать свой текст / файл", callback_data="edit_custom_title_tmpl"),
                InlineKeyboardButton(text="🔄 Сбросить к образцу МТИ", callback_data="reset_title_tmpl_mti")
            ],
            [
                InlineKeyboardButton(text="🔙 Назад в профиль", callback_data="cancel_profile")
            ]
        ]
    )
    await safe_edit(call.message, text, reply_markup=kb)


@router.callback_query(F.data == "edit_custom_title_tmpl")
async def cb_edit_custom_title_tmpl(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["waiting_input"] = "custom_title_template"
    user_sessions[user_id]["active_card_msg"] = call.message

    await call.answer()
    await safe_edit(
        call.message,
        "✍️ **Отправьте текст или файл с новым образцом титульника** (`.docx`, `.pdf` или фото):\n\n"
        "Он будет сохранен в вашем профиле как постоянный шаблон.",
        reply_markup=get_cancel_keyboard("profile")
    )


@router.callback_query(F.data == "reset_title_tmpl_mti")
async def cb_reset_title_tmpl_mti(call: CallbackQuery):
    user_id = call.from_user.id
    from database import DEFAULT_TITLE_TEMPLATE
    update_profile(user_id, custom_title_template=DEFAULT_TITLE_TEMPLATE)
    await call.answer("Шаблон сброшен к стандартному МТИ!")
    prof = get_profile(user_id)
    anti_str = "ВКЛ (высокая оригинальность)" if prof.get("antiplagiat_mode", 1) else "ВЫКЛ"
    text = (
        "👤 **Ваш студенческий профиль:**\n\n"
        f"• **ФИО студента:** {prof.get('full_name') or '*(не указано)*'}\n"
        f"• **Группа:** {prof.get('group_name') or '*(не указано)*'}\n"
        f"• **ВУЗ / Организация:** {prof.get('university') or '*(не указано)*'}\n"
        f"• **Преподаватель:** {prof.get('teacher') or '*(не указано)*'}\n"
        f"• **Дисциплина:** {prof.get('discipline') or '*(не указано)*'}\n"
        f"• **Вариант по умолчанию:** {prof.get('variant') or '*(любой)*'}\n"
        f"• **Шаблон титульника:** МТИ (зафиксирован)\n"
        f"• **Режим Антиплагиат:** {anti_str}\n\n"
        "💡 *Эти данные автоматически подставляются в титульный лист.*\n"
        "Нажмите кнопку ниже, чтобы изменить нужное поле:"
    )
    await safe_edit(call.message, text, reply_markup=get_profile_keyboard(prof))


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
            "💡 *Эти данные автоматически подставляются в титульный лист.*\n"
            "Нажмите кнопку ниже, чтобы изменить нужное поле:"
        )
        await safe_edit(call.message, text, reply_markup=get_profile_keyboard(prof))
    elif back_to == "edit":
        await safe_delete_msg(call.bot, call.message.chat.id, call.message.message_id)
    elif back_to == "style":
        text = get_style_menu_text(prof)
        kb = get_style_keyboard(prof.get("active_style_preset", "standard"))
        await safe_edit(call.message, text, reply_markup=kb)
    else:
        session = user_sessions.get(user_id, {})
        custom = session.get("custom_instructions", "")
        sample_name = session.get("sample_filename", "")
        await safe_edit(
            call.message, 
            "✅ **Параметры работы:**\nНажмите **«Сгенерировать отчет»** для запуска:", 
            reply_markup=get_options_keyboard(prof, custom, sample_name=sample_name)
        )


@router.callback_query(F.data == "close_profile")
async def cb_close_profile(call: CallbackQuery):
    await call.answer("Профиль сохранен!")
    await safe_delete_msg(call.bot, call.message.chat.id, call.message.message_id)


@router.callback_query(F.data == "open_profile")
async def cb_open_profile(call: CallbackQuery):
    user_id = call.from_user.id
    prof = get_profile(user_id)
    anti_str = "ВКЛ (высокая оригинальность 85%+)" if prof.get("antiplagiat_mode", 1) else "ВЫКЛ (базовый)"
    preset_key = prof.get("active_style_preset", "standard")
    preset_info = STYLE_PRESETS.get(preset_key, STYLE_PRESETS["standard"])

    text = (
        "👤 **Ваш студенческий профиль:**\n\n"
        f"• **ФИО студента:** {prof.get('full_name') or '*(не указано)*'}\n"
        f"• **Учебная группа:** {prof.get('group_name') or '*(не указано)*'}\n"
        f"• **ВУЗ / Организация:** {prof.get('university') or '*(не указано)*'}\n"
        f"• **Преподаватель:** {prof.get('teacher') or '*(не указано)*'}\n"
        f"• **Дисциплина:** {prof.get('discipline') or '*(не указано)*'}\n"
        f"• **Вариант по умолчанию:** {prof.get('variant') or '*(любой)*'}\n\n"
        "🏛️ **Оформление титульного листа:**\n"
        "• **Стандарт:** МОСКОВСКИЙ ТЕХНОЛОГИЧЕСКИЙ ИНСТИТУТ (МТИ) ✅\n"
        "• *Титульник создается автоматически по зафиксированному образцу.*\n\n"
        f"🎨 **Стиль оформления:** {preset_info['title']}\n"
        f"🛡️ **Антиплагиат:** {anti_str}\n\n"
        "👇 *Нажмите кнопку ниже, чтобы изменить нужное поле:*"
    )
    await safe_edit(call.message, text, reply_markup=get_profile_keyboard(prof))


@router.callback_query(F.data == "open_style_menu")
async def cb_open_style_menu(call: CallbackQuery):
    user_id = call.from_user.id
    prof = get_profile(user_id)
    text = get_style_menu_text(prof)
    kb = get_style_keyboard(prof.get("active_style_preset", "standard"))
    await safe_edit(call.message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("style_set_"))
async def cb_style_set_preset(call: CallbackQuery):
    user_id = call.from_user.id
    preset = call.data.replace("style_set_", "")
    update_profile(user_id, active_style_preset=preset)

    if preset == "repeat_last":
        last_lab = get_last_lab(user_id)
        if last_lab and last_lab.get("instructions"):
            update_profile(user_id, last_custom_instructions=last_lab.get("instructions"))
            if user_id in user_sessions:
                user_sessions[user_id]["custom_instructions"] = last_lab.get("instructions")
    elif preset == "qa_only":
        qa_rule = "Без введения, без заключения, просто вопрос и ответ на него."
        update_profile(user_id, last_custom_instructions=qa_rule)
        if user_id in user_sessions:
            user_sessions[user_id]["custom_instructions"] = qa_rule
    elif preset == "practice_only":
        update_profile(user_id, omit_theory=1)

    await call.answer("Формат обновлен!")
    prof = get_profile(user_id)
    text = get_style_menu_text(prof)
    kb = get_style_keyboard(prof.get("active_style_preset", "standard"))
    await safe_edit(call.message, text, reply_markup=kb)


@router.callback_query(F.data == "style_enter_custom")
async def cb_style_enter_custom(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["waiting_input"] = "custom_instructions"
    user_sessions[user_id]["active_card_msg"] = call.message

    await call.answer()
    await safe_edit(
        call.message,
        "✍️ **Напишите ваши пожелания к стилю отчета:**\n\n"
        "(например: *«без введения и заключения, просто вопрос и ответ на него»*, "
        "*«сделай вариант 5»*, *«оформи кратко без теории»*)\n\n"
        "Бот сохранит их в памяти и будет использовать для ваших отчетов.",
        reply_markup=get_cancel_keyboard("style")
    )


@router.callback_query(F.data == "style_back_to_options")
async def cb_style_back_to_options(call: CallbackQuery):
    user_id = call.from_user.id
    prof = get_profile(user_id)
    session = user_sessions.get(user_id, {})
    custom = session.get("custom_instructions") or prof.get("last_custom_instructions", "")
    sample_name = session.get("sample_filename", "")

    if session.get("lab_text"):
        preview_text = session["lab_text"][:280] + ("..." if len(session["lab_text"]) > 280 else "")
        preset_info = STYLE_PRESETS.get(prof.get("active_style_preset", "standard"), STYLE_PRESETS["standard"])
        student_str = f"{prof.get('full_name')} ({prof.get('group_name')})"
        card_text = (
            f"📄 **Задание к лабораторной работе:**\n\n"
            f"👥 **Студент:** {student_str}\n"
            f"🏛️ **Титульный лист:** МТИ (преп. {prof.get('teacher')})\n"
            f"🎨 **Формат отчета:** {preset_info['title']}\n"
            f"{f'💬 *Пожелания:* _{custom}_' + chr(10) if custom else ''}\n"
            f"📖 *Фрагмент задания:*\n_{preview_text}_\n\n"
            f"Нажмите **«Сгенерировать отчет»** для запуска:"
        )
        await safe_edit(call.message, card_text, reply_markup=get_options_keyboard(prof, custom, sample_name=sample_name))
    else:
        # Если методички еще нет, показываем профиль или главное меню
        welcome_text = (
            "🎓 **Главное меню**\n\n"
            f"• 🏛️ **Титульный лист:** МТИ (зафиксирован)\n"
            f"• 🎨 **Формат отчета:** {STYLE_PRESETS.get(prof.get('active_style_preset', 'standard'))['title']}\n\n"
            "Отправьте **файл методички** или **фото задания**, чтобы начать сборку отчета."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="👤 Профиль и титульник", callback_data="open_profile"),
                    InlineKeyboardButton(text="🧠 Память стиля", callback_data="open_style_menu")
                ],
                [
                    InlineKeyboardButton(text="📚 Мои отчеты", callback_data="open_my_labs"),
                    InlineKeyboardButton(text="❓ Как пользоваться", callback_data="open_help")
                ]
            ]
        )
        await safe_edit(call.message, welcome_text, reply_markup=kb)


@router.callback_query(F.data == "open_my_labs")
async def cb_open_my_labs(call: CallbackQuery):
    user_id = call.from_user.id
    labs = get_user_labs(user_id, limit=8)

    if not labs:
        await call.answer("У вас пока нет сохраненных отчетов.", show_alert=True)
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

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="close_labs")])
    await safe_edit(call.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "open_help")
async def cb_open_help(call: CallbackQuery):
    text = (
        "📖 **Инструкция по использованию бота:**\n\n"
        "1️⃣ **Отправка задания:**\n"
        "Прикрепите методичку (`.docx`, `.pdf`, `.txt`) или фото страниц задания прямо в чат.\n\n"
        "2️⃣ **Память стиля и пожелания:**\n"
        "Вы можете в любой момент написать: *«сделай так же, как предыдущую»* или "
        "*«без введения и заключения, только вопросы и ответы»* — бот запомнит это и применит к отчету.\n\n"
        "3️⃣ **Титульный лист:**\n"
        "По умолчанию установлен стандарт МТИ с вашими данными. Если титульник не нужен, просто нажмите кнопку `🎓 Титульник: Выкл`.\n\n"
        "4️⃣ **Правки:**\n"
        "После получения отчета напишите замечания в чат (например: *«замени преподавателя»*, *«убери график»*), и бот мгновенно пересоберет документ."
    )
    await safe_edit(call.message, text, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Понятно", callback_data="close_profile")]]
    ))


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
        "✍️ **Индивидуальные пожелания и образец оформления:**\n\n"
        "Вы можете:\n"
        "• **Написать требования текстом** (например: *«без введения и заключения, только вопросы и ответы»*, *«сделай вариант 5»*).\n"
        "• **ИЛИ отправить файл / фото образца титульника** (`.docx`, `.pdf` или фото/скриншот).\n\n"
        "🎓 *Нейросеть изучит пожелания и оформит работу в точности по вашим указаниям!*",
        reply_markup=get_cancel_keyboard("options")
    )


@router.callback_query(F.data == "clear_custom_text")
async def cb_clear_custom_text(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id in user_sessions:
        user_sessions[user_id]["custom_instructions"] = ""
        user_sessions[user_id]["sample_filename"] = ""
        user_sessions[user_id]["waiting_input"] = None
    update_profile(user_id, last_custom_instructions="", active_style_preset="standard")
    await call.answer("Пожелания сброшены")
    prof = get_profile(user_id)
    session = user_sessions.get(user_id, {})
    if session.get("lab_text"):
        preview_text = session["lab_text"][:280] + ("..." if len(session["lab_text"]) > 280 else "")
        student_str = f"{prof.get('full_name')} ({prof.get('group_name')})"
        card_text = (
            f"📄 **Задание к лабораторной работе:**\n\n"
            f"👥 **Студент:** {student_str}\n"
            f"🏛️ **Титульный лист:** МТИ (преп. {prof.get('teacher')})\n"
            f"🎨 **Формат отчета:** 📝 Полный ГОСТ\n\n"
            f"📖 *Фрагмент задания:*\n_{preview_text}_\n\n"
            f"Нажмите **«Сгенерировать отчет»** для запуска:"
        )
        await safe_edit(call.message, card_text, reply_markup=get_options_keyboard(prof, ""))
    else:
        await safe_edit(
            call.message,
            "✅ **Пожелания сброшены к стандартным.**\n\n"
            "Отправьте методичку или фото задания для генерации отчета.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧠 Настроить стиль", callback_data="open_style_menu")]
            ])
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
    prof = get_profile(user_id)

    # ВСЕГДА удаляем входящее текстовое сообщение пользователя,
    # чтобы при заполнении профиля, варианта, правок и т.д. оно не висело в чате!
    await safe_delete_msg(bot, chat_id, message.message_id)

    # Проверка: ожидается ли ввод поля профиля или пожеланий
    if user_id in user_sessions and user_sessions[user_id].get("waiting_input"):
        field = user_sessions[user_id]["waiting_input"]
        user_sessions[user_id]["waiting_input"] = None
        card_msg = user_sessions[user_id].get("active_card_msg")

        if field == "edit_text":
            status_msg = await message.answer(
                f"🔄 **Вношу правки в отчет:** _{user_text}_\n\n"
                "⚡ Пересобираю документ Word и обновляю PDF..."
            )
            track_message(user_id, status_msg.message_id)
            await process_edit(message, status_msg, user_id, user_text)
            return

        if field == "custom_instructions":
            user_sessions[user_id]["custom_instructions"] = user_text
            inst_l = user_text.lower()
            if any(k in inst_l for k in ["вопрос и ответ", "вопросы и ответы", "без введения", "без заключения", "только вопрос"]):
                update_profile(user_id, active_style_preset="qa_only", last_custom_instructions=user_text)
            elif any(k in inst_l for k in ["как прошл", "как предыдущ", "как в прошлый раз"]):
                update_profile(user_id, active_style_preset="repeat_last", last_custom_instructions=user_text)
            elif any(k in inst_l for k in ["только практика", "без теории"]):
                update_profile(user_id, active_style_preset="practice_only", last_custom_instructions=user_text, omit_theory=1)
            else:
                update_profile(user_id, last_custom_instructions=user_text)

            sample_name = user_sessions[user_id].get("sample_filename", "")
            updated_prof = get_profile(user_id)
            preset_info = STYLE_PRESETS.get(updated_prof.get("active_style_preset", "standard"), STYLE_PRESETS["custom"])

            if card_msg:
                student_str = f"{updated_prof.get('full_name')} ({updated_prof.get('group_name')})"
                preview_text = user_sessions[user_id].get("lab_text", "")[:280]
                card_text = (
                    f"📄 **Параметры отчета обновлены!**\n\n"
                    f"👥 **Студент:** {student_str}\n"
                    f"🏛️ **Титульный лист:** МТИ (преп. {updated_prof.get('teacher')})\n"
                    f"🎨 **Формат отчета:** {preset_info['title']}\n"
                    f"💬 *Пожелания:* _{user_text}_\n\n"
                    f"{f'📖 *Фрагмент методички:*' + chr(10) + f'_{preview_text}_...' + chr(10) + chr(10) if preview_text else ''}"
                    f"Нажмите **«Сгенерировать отчет»** для запуска:"
                )
                await safe_edit(card_msg, card_text, reply_markup=get_options_keyboard(updated_prof, user_text, sample_name=sample_name))
            else:
                sent = await message.answer(
                    f"🧠 **Пожелания сохранены в памяти:**\n_{user_text}_\n"
                    f"🎨 **Формат:** {preset_info['title']}\n\n"
                    "Отправьте методичку (`.docx`, `.pdf`, `.txt`) или фото задания для генерации отчета!",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎨 Настроить стиль", callback_data="open_style_menu")]
                    ]),
                    parse_mode="Markdown"
                )
                track_message(user_id, sent.message_id)
            return
        elif field == "custom_title_template":
            update_profile(user_id, custom_title_template=user_text)
            updated_prof = get_profile(user_id)
            anti_str = "ВКЛ (высокая оригинальность)" if updated_prof.get("antiplagiat_mode", 1) else "ВЫКЛ"
            text = (
                "👤 **Ваш студенческий профиль:**\n\n"
                f"• **ФИО студента:** {updated_prof.get('full_name') or '*(не указано)*'}\n"
                f"• **Группа:** {updated_prof.get('group_name') or '*(не указано)*'}\n"
                f"• **ВУЗ / Организация:** {updated_prof.get('university') or '*(не указано)*'}\n"
                f"• **Преподаватель:** {updated_prof.get('teacher') or '*(не указано)*'}\n"
                f"• **Дисциплина:** {updated_prof.get('discipline') or '*(не указано)*'}\n"
                f"• **Вариант по умолчанию:** {updated_prof.get('variant') or '*(любой)*'}\n"
                f"• **Шаблон титульника:** Сохранен индивидуальный\n"
                f"• **Режим Антиплагиат:** {anti_str}\n\n"
                "✅ *Новый постоянный образец титульника успешно сохранен!*\n"
                "Нажмите кнопку ниже, чтобы изменить нужное поле:"
            )
            if card_msg:
                await safe_edit(card_msg, text, reply_markup=get_profile_keyboard(updated_prof))
            return
        elif field in ["full_name", "group_name", "university", "teacher", "discipline", "variant"]:
            update_profile(user_id, **{field: user_text})
            updated_prof = get_profile(user_id)
            anti_str = "ВКЛ (высокая оригинальность)" if updated_prof.get("antiplagiat_mode", 1) else "ВЫКЛ"
            text = (
                "👤 **Ваш студенческий профиль:**\n\n"
                f"• **ФИО студента:** {updated_prof.get('full_name') or '*(не указано)*'}\n"
                f"• **Группа:** {updated_prof.get('group_name') or '*(не указано)*'}\n"
                f"• **ВУЗ / Кафедра:** {updated_prof.get('university') or '*(не указано)*'}\n"
                f"• **Преподаватель:** {updated_prof.get('teacher') or '*(не указано)*'}\n"
                f"• **Дисциплина:** {updated_prof.get('discipline') or '*(не указано)*'}\n"
                f"• **Вариант по умолчанию:** {updated_prof.get('variant') or '*(любой)*'}\n"
                f"• **Режим Антиплагиат:** {anti_str}\n\n"
                "💡 *Эти данные автоматически подставляются в титульный лист.*\n"
                "Нажмите кнопку ниже, чтобы изменить нужное поле:"
            )
            if card_msg:
                await safe_edit(card_msg, text, reply_markup=get_profile_keyboard(updated_prof))
            return

    if user_id not in user_sessions or not user_sessions[user_id].get("lab_text"):
        session = user_sessions.setdefault(user_id, {})
        session["custom_instructions"] = user_text
        
        inst_l = user_text.lower()
        if any(k in inst_l for k in ["вопрос и ответ", "вопросы и ответы", "без введения", "без заключения", "только вопрос", "просто вопрос"]):
            update_profile(user_id, active_style_preset="qa_only", last_custom_instructions=user_text)
        elif any(k in inst_l for k in ["как прошл", "как предыдущ", "как в прошлый раз", "такой же как", "по аналогии"]):
            update_profile(user_id, active_style_preset="repeat_last")
            last_lab = get_last_lab(user_id)
            if last_lab and last_lab.get("instructions"):
                session["custom_instructions"] = last_lab.get("instructions")
                update_profile(user_id, last_custom_instructions=last_lab.get("instructions"))
            else:
                update_profile(user_id, last_custom_instructions=user_text)
        elif any(k in inst_l for k in ["только практика", "без теории"]):
            update_profile(user_id, active_style_preset="practice_only", last_custom_instructions=user_text, omit_theory=1)
        else:
            update_profile(user_id, last_custom_instructions=user_text)

        updated_prof = get_profile(user_id)
        preset_info = STYLE_PRESETS.get(updated_prof.get("active_style_preset", "standard"), STYLE_PRESETS["custom"])

        sent = await message.answer(
            f"🧠 **Пожелания сохранены в памяти бота!**\n\n"
            f"💬 *«{user_text}»*\n"
            f"🎨 **Формат отчета:** {preset_info['title']}\n\n"
            "📌 **Что делать дальше:**\n"
            "Отправьте **файл методички** (`.docx`, `.pdf`, `.txt`) или **фото задания** сюда в чат — "
            "и отчет будет собран строго в соответствии с вашими требованиями!",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎨 Настроить стиль", callback_data="open_style_menu")],
                    [InlineKeyboardButton(text="🗑️ Сбросить пожелания", callback_data="clear_custom_text")]
                ]
            ),
            parse_mode="Markdown"
        )
        track_message(user_id, sent.message_id)
        return

    session = user_sessions[user_id]
    has_previous_code = bool(session.get("last_code"))

    if not has_previous_code:
        session["custom_instructions"] = user_text
        inst_l = user_text.lower()
        if any(k in inst_l for k in ["вопрос и ответ", "без введения", "без заключения"]):
            update_profile(user_id, active_style_preset="qa_only", last_custom_instructions=user_text)
        elif any(k in inst_l for k in ["как прошл", "как предыдущ"]):
            update_profile(user_id, active_style_preset="repeat_last")
        else:
            update_profile(user_id, last_custom_instructions=user_text)

        status_msg = await message.answer(
            f"⚡ **Пожелание принято:** _{user_text}_\n\n"
            "Нейросеть решает задачи и формирует структуру работы..."
        )
        track_message(user_id, status_msg.message_id)
        await process_generation(message, status_msg, user_id, user_text)
    else:
        status_msg = await message.answer(
            f"🔄 **Вношу правки в отчет:** _{user_text}_\n\n"
            "⚡ Пересобираю документ Word и обновляю PDF..."
        )
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
            add_lab_history(
                user_id, 
                title=disc, 
                discipline=disc, 
                docx_path=docx_path, 
                pdf_path=pdf_path,
                instructions=instructions,
                style_preset=prof.get("active_style_preset", "standard")
            )
            if instructions:
                update_profile(user_id, last_custom_instructions=instructions)

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
        new_code = await llm_service.edit_existing_code(old_code, edit_instructions, profile=prof)
        await safe_edit(status_msg, "⚡ Пересобираю документ Word и обновляю PDF...")
        success, docx_path, pdf_path, chart_paths, error_msg = await execute_with_auto_fix(new_code, user_dir, status_msg)

        if success and docx_path and os.path.exists(docx_path):
            session["last_code"] = new_code
            session["last_docx"] = docx_path
            
            disc = prof.get("discipline") or "Лабораторная работа"
            add_lab_history(
                user_id, 
                title=f"{disc} (с правками)", 
                discipline=disc, 
                docx_path=docx_path, 
                pdf_path=pdf_path,
                instructions=edit_instructions,
                style_preset=prof.get("active_style_preset", "standard")
            )
            if edit_instructions:
                update_profile(user_id, last_custom_instructions=edit_instructions)

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
    prof = get_profile(user_id)
    preset_key = prof.get("active_style_preset", "standard")
    preset_info = STYLE_PRESETS.get(preset_key, STYLE_PRESETS["standard"])
    student_str = f"{prof.get('full_name')} ({prof.get('group_name')})"

    caption_docx = (
        f"✅ **Ваш отчет успешно {action_word}!**\n\n"
        f"📎 **Формат:** Microsoft Word (`.docx`) + PDF\n"
        f"🏛️ **Титульный лист:** МТИ ({student_str})\n"
        f"🎨 **Стиль работы:** {preset_info['title']}\n"
        f"📐 **Стандарт:** ГОСТ (поля 30/15/20/20, строгий черный текст, таблицы)\n\n"
        f"💬 **Нужны правки?** Напишите замечания в чат (например: *«исправь формулу 2»*, *«убери лишнюю теорию»*) "
        f"или воспользуйтесь кнопками ниже."
    )
    post_gen_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Внести правки текстом", callback_data="post_edit_text"),
                InlineKeyboardButton(text="📎 Другой титульник", callback_data="post_attach_sample")
            ],
            [
                InlineKeyboardButton(text="🔄 Сделать следующую в таком же стиле", callback_data="post_repeat_style")
            ],
            [
                InlineKeyboardButton(text="🆕 Сдать другую работу", callback_data="post_new_lab")
            ]
        ]
    )
    await message.answer_document(
        document=FSInputFile(docx_path, filename="Laboratornaya_Rabota.docx"),
        caption=caption_docx,
        reply_markup=post_gen_kb,
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


# -------------------------------------------------------------
# Callbacks после генерации и выбора действия с файлами
# -------------------------------------------------------------
@router.callback_query(F.data == "post_attach_sample")
async def cb_post_attach_sample(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["waiting_input"] = "edit_sample"
    user_sessions[user_id]["active_card_msg"] = call.message
    await call.answer()
    sent = await call.message.answer(
        "📎 **Отправьте файл или фото-образец титульного листа** (`.docx`, `.pdf`, фото):\n\n"
        "Нейросеть прочитает образец и пересоберет текущий отчет с новым титульником.",
        reply_markup=get_cancel_keyboard("edit")
    )
    track_message(user_id, sent.message_id)


@router.callback_query(F.data == "post_edit_text")
async def cb_post_edit_text(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]["waiting_input"] = "edit_text"
    user_sessions[user_id]["active_card_msg"] = call.message
    await call.answer()
    sent = await call.message.answer(
        "✏️ **Напишите ваши замечания и правки к отчету:**\n"
        "(например: *«замени преподавателя на Сидорова»*, *«убери лишнюю теорию»*, *«исправь формулу 2»*)",
        reply_markup=get_cancel_keyboard("edit")
    )
    track_message(user_id, sent.message_id)


@router.callback_query(F.data == "post_repeat_style")
async def cb_post_repeat_style(call: CallbackQuery):
    user_id = call.from_user.id
    prof = get_profile(user_id)
    session = user_sessions.get(user_id, {})
    current_inst = session.get("custom_instructions") or prof.get("last_custom_instructions", "")

    if current_inst:
        update_profile(user_id, last_custom_instructions=current_inst)
    update_profile(user_id, active_style_preset="repeat_last")

    user_sessions[user_id] = {
        "lab_text": "",
        "last_code": "",
        "custom_instructions": current_inst,
        "sample_filename": session.get("sample_filename", ""),
        "waiting_input": None,
        "session_dir": get_user_dir(user_id)
    }

    await call.answer("Стиль зафиксирован!")
    sent = await call.message.answer(
        "🔄 **Стиль успешно зафиксирован для следующей работы!**\n\n"
        f"💬 *Формат:* {current_inst or 'Как в этой работе'}\n\n"
        "Отправьте **файл новой методички** (`.docx`, `.pdf`, `.txt`) или **фото задания** — "
        "и я сделаю следующую работу в точности так же!",
        parse_mode="Markdown"
    )
    track_message(user_id, sent.message_id)


@router.callback_query(F.data == "post_new_lab")
async def cb_post_new_lab(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id in user_sessions:
        user_sessions[user_id]["lab_text"] = ""
        user_sessions[user_id]["last_code"] = ""
        user_sessions[user_id]["custom_instructions"] = ""
        user_sessions[user_id]["sample_filename"] = ""
        user_sessions[user_id]["waiting_input"] = None
    await call.answer()
    sent = await call.message.answer(
        "🆕 **Готов к новой лабораторной работе!**\n\n"
        "Отправьте **файл методички** (`.pdf`, `.docx`, `.txt`) или **фото задания**."
    )
    track_message(user_id, sent.message_id)


@router.callback_query(F.data == "choice_use_as_sample")
async def cb_choice_use_sample(call: CallbackQuery):
    user_id = call.from_user.id
    session = user_sessions.get(user_id, {})
    pending = session.get("pending_new_doc")
    if not pending:
        await call.answer("Файл не найден.", show_alert=True)
        return

    await call.answer()
    local_path = pending["path"]
    filename = pending["filename"]
    caption = pending.get("caption", "")
    session["pending_new_doc"] = None

    status_msg = call.message
    await safe_edit(status_msg, f"📥 Изучаю образец оформления из `{filename}`...")

    sample_text = await extract_text_from_file(local_path, max_chars=config.MAX_PROMPT_CHARS)
    sample_instruction = (
        f"ОБРАЗЕЦ ТИТУЛЬНОГО ЛИСТА / ОФОРМЛЕНИЯ ИЗ ФАЙЛА «{filename}»:\n"
        f"Полностью перепиши титульный лист в точном соответствии с присланным образцом (шапка, названия министерства/ВУЗа, кафедры, формулировки блоков Выполнил/Проверил, город, год):\n\n"
        f"{sample_text.strip()}"
    )
    if caption:
        sample_instruction = f"{caption}\n\n{sample_instruction}"

    await safe_edit(status_msg, f"🔄 **Применяю образец титульника `{filename}`...**\n⚡ Пересобираю документ Word и обновляю PDF...")
    await process_edit(call.message, status_msg, user_id, sample_instruction)


@router.callback_query(F.data == "choice_start_new_lab")
async def cb_choice_new_lab(call: CallbackQuery):
    user_id = call.from_user.id
    session = user_sessions.get(user_id, {})
    pending = session.get("pending_new_doc")
    if not pending:
        await call.answer("Файл не найден.", show_alert=True)
        return

    await call.answer()
    local_path = pending["path"]
    filename = pending["filename"]
    caption = pending.get("caption", "")
    session["pending_new_doc"] = None

    status_msg = call.message
    await safe_edit(status_msg, f"📥 Читаю задание из `{filename}`...")

    extracted_text = await extract_text_from_file(local_path, max_chars=config.MAX_PROMPT_CHARS)
    user_dir = get_user_dir(user_id)
    user_sessions[user_id] = {
        "lab_text": extracted_text,
        "session_dir": user_dir,
        "last_code": "",
        "last_docx": "",
        "waiting_input": None,
        "card_msg_id": status_msg.message_id,
        "active_card_msg": status_msg,
        "clean_stack": [status_msg.message_id]
    }
    if caption:
        user_sessions[user_id]["custom_instructions"] = caption

    prof = get_profile(user_id)
    preview_text = extracted_text[:300] + ("..." if len(extracted_text) > 300 else "")
    await safe_edit(
        status_msg,
        f"✅ **Методичка `{filename}` прочитана!**\n\n"
        f"📖 *Фрагмент задания:*\n_{preview_text}_\n\n"
        f"Нажмите **«Сгенерировать отчет»** для запуска:",
        reply_markup=get_options_keyboard(prof, "")
    )


@router.callback_query(F.data == "cancel_file_choice")
async def cb_cancel_file_choice(call: CallbackQuery):
    user_id = call.from_user.id
    if user_id in user_sessions:
        user_sessions[user_id]["pending_new_doc"] = None
    await call.answer("Отменено")
    await safe_delete_msg(call.bot, call.message.chat.id, call.message.message_id)
