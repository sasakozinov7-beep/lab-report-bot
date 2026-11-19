import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import config
from handlers import router

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("lab_bot")


async def main():
    logger.info("Запуск Telegram-бота генерации отчетов по лабораторным работам...")

    # Валидация конфигурации
    if not config.BOT_TOKEN or config.BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error(
            "ОШИБКА: Токен бота не указан в файле .env (параметр BOT_TOKEN)!\n"
            "1. Создайте файл .env на основе .env.example\n"
            "2. Укажите BOT_TOKEN, полученный у @BotFather"
        )
        sys.exit(1)

    if config.LLM_PROVIDER == "openai":
        if not config.LLM_API_KEY:
            logger.warning(
                "ВНИМАНИЕ: LLM_API_KEY не указан в .env для провайдера OpenAI / OpenRouter / DeepSeek! "
                "Бот не сможет отправлять запросы к нейросети."
            )
    elif config.LLM_PROVIDER == "gemini":
        if not config.GEMINI_API_KEY:
            logger.warning(
                "ВНИМАНИЕ: GEMINI_API_KEY не указан в .env для провайдера Gemini! "
                "Бот не сможет отправлять запросы к нейросети."
            )

    # Инициализация бота и диспетчера
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )
    dp = Dispatcher()

    # Регистрация команд в меню Telegram
    commands = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="profile", description="👤 Профиль и титульник (МТИ)"),
        BotCommand(command="style", description="🧠 Память стиля и пресеты"),
        BotCommand(command="my_labs", description="📚 Мои отчеты"),
        BotCommand(command="help", description="❓ Как пользоваться"),
        BotCommand(command="reset", description="🔄 Сбросить работу")
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Не удалось установить команды: {e}")

    # Регистрация обработчиков
    dp.include_router(router)

    # Пропуск накопившихся апдейтов при выключенном боте
    await bot.delete_webhook(drop_pending_updates=True)

    bot_info = await bot.get_me()
    logger.info(f"Бот @{bot_info.username} успешно запущен и готов к приему сообщений!")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Программа прервана пользователем.")
