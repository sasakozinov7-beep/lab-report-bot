import re
import os
import logging
from typing import Optional, Dict, Any, List
import PIL.Image

from config import config
from prompts import SYSTEM_PROMPT_GENERATOR, SYSTEM_PROMPT_EDITOR, SYSTEM_PROMPT_FIXER

logger = logging.getLogger(__name__)


class LLMService:
    def __init__(self):
        self.provider = config.LLM_PROVIDER.lower()
        if self.provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=config.GEMINI_API_KEY)
            self.gemini_model = genai.GenerativeModel(
                model_name=config.GEMINI_MODEL,
                generation_config={
                    "temperature": 0.2,
                    "top_p": 0.95,
                    "max_output_tokens": 8192
                }
            )
        else:
            from openai import AsyncOpenAI
            self.openai_client = AsyncOpenAI(
                api_key=config.LLM_API_KEY,
                base_url=config.LLM_BASE_URL
            )

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """Асинхронный вызов выбранной модели нейросети."""
        if self.provider == "gemini":
            full_prompt = f"{system_prompt}\n\nПользовательский запрос:\n{user_prompt}"
            response = await self.gemini_model.generate_content_async(full_prompt)
            return response.text
        else:
            response = await self.openai_client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2
            )
            return response.choices[0].message.content or ""

    async def extract_text_from_images(self, image_paths: List[str]) -> str:
        """Распознавание текста и заданий на нескольких изображениях или сканах (например, из DOCX/PDF)."""
        if not image_paths:
            return ""
        if len(image_paths) == 1:
            return await self.extract_text_from_image(image_paths[0])
            
        try:
            logger.info(f"Распознавание группы из {len(image_paths)} изображений через Gemini Vision...")
            parts = []
            for path in image_paths[:8]:
                try:
                    img = PIL.Image.open(path)
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    if max(img.size) > 1200:
                        img.thumbnail((1200, 1200), PIL.Image.Resampling.BILINEAR)
                    parts.append(img)
                except Exception as img_err:
                    logger.warning(f"Ошибка открытия изображения {path}: {img_err}")

            if not parts:
                return ""

            prompt = (
                "Ты инженер-методист. Проанализируй страницы методички/задания и составь подробное ТЗ для выполнения лабораторной работы: "
                "тема, цель, краткие теоретические сведения, исходные числовые данные, формулы, порядок выполнения, таблицы и контрольные вопросы. "
                "Сохрани все числовые значения, формулы и варианты без искажений."
            )
            parts.append(prompt)

            if self.provider == "gemini":
                resp = await self.gemini_model.generate_content_async(parts)
                if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
                    return "".join(p.text for p in resp.candidates[0].content.parts if hasattr(p, "text") and p.text).strip()
                return resp.text.strip()
            else:
                return "Изображения получены, но распознавание настроено для Gemini."
        except Exception as e:
            logger.exception("Ошибка при распознавании группы изображений")
            return f"Ошибка распознавания фото: {e}"

    async def extract_text_from_image(self, image_path: str) -> str:
        """Распознавание текста и заданий на фотографии методички через Multimodal Vision."""
        try:
            logger.info(f"Распознавание текста на изображении: {image_path}")
            img = PIL.Image.open(image_path)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            # Быстрое сжатие до оптимального разрешения для мгновенной передачи
            if max(img.size) > 1200:
                img.thumbnail((1200, 1200), PIL.Image.Resampling.BILINEAR)
            prompt = (
                "Ты инженер-методист. Проанализируй изображение методички/задания и составь подробное ТЗ для выполнения лабораторной работы: "
                "тема, цель, краткие теоретические сведения, исходные числовые данные, формулы, порядок выполнения, таблицы и контрольные вопросы. "
                "Сохрани все числовые значения, формулы и варианты без искажений."
            )
            if self.provider == "gemini":
                resp = await self.gemini_model.generate_content_async([img, prompt])
                if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
                    return "".join(p.text for p in resp.candidates[0].content.parts if hasattr(p, "text") and p.text).strip()
                return resp.text.strip()
            else:
                return "Изображение получено, но распознавание настроено для Gemini."
        except Exception as e:
            logger.exception("Ошибка при распознавании изображения")
            return f"Ошибка распознавания фото: {e}"

    async def extract_sample_title_page(self, image_path: str) -> str:
        """Извлечение структуры и текста образца титульного листа из фотографии/скриншота."""
        try:
            logger.info(f"Распознавание образца титульного листа: {image_path}")
            img = PIL.Image.open(image_path)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if max(img.size) > 1200:
                img.thumbnail((1200, 1200), PIL.Image.Resampling.BILINEAR)
            prompt = (
                "Ты эксперт по академическому оформлению отчетов. Перед тобой образец титульного листа или оформления работы. "
                "Детально и построчно выпиши все элементы: "
                "1. Точную шапку (Министерство, полное наименование ВУЗа, факультет/институт, кафедра). "
                "2. Заголовок работы, дисциплину, тему и номер лабораторной. "
                "3. Расположение и формулировку блоков исполнителя и проверяющего (например: 'Выполнил: студент гр. ..., ФИО', 'Проверил: ученая степень, должность, ФИО'). "
                "4. Город и год внизу. "
                "5. Все особенности разметки (выравнивание, отступы, регистр). "
                "Сохрани все формулировки максимально точно, чтобы скрипт Word docx мог воспроизвести этот титульник."
            )
            if self.provider == "gemini":
                resp = await self.gemini_model.generate_content_async([img, prompt])
                if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
                    return "".join(p.text for p in resp.candidates[0].content.parts if hasattr(p, "text") and p.text).strip()
                return resp.text.strip()
            else:
                return "Распознан образец оформления."
        except Exception as e:
            logger.exception("Ошибка при распознавании образца титульника")
            return f"Образец оформления: {e}"

    def _extract_code(self, raw_response: str) -> str:
        """Извлекает чистый Python код из markdown-блока ```python ... ```."""
        pattern = r"```(?:python)?(.*?)```"
        matches = re.findall(pattern, raw_response, re.DOTALL)
        if matches:
            return matches[0].strip()
        return raw_response.strip()

    async def generate_initial_code(
        self, 
        lab_text: str, 
        user_instructions: str,
        profile: Optional[Dict[str, Any]] = None
    ) -> str:
        """Генерация первичного скрипта с учетом профиля студента, варианта и антиплагиата."""
        profile_details = ""
        if profile:
            items = []
            if profile.get("full_name"):
                items.append(f"• ФИО Студента: {profile['full_name']}")
            if profile.get("group_name"):
                items.append(f"• Группа: {profile['group_name']}")
            if profile.get("university"):
                items.append(f"• ВУЗ / Кафедра: {profile['university']}")
            if profile.get("teacher"):
                items.append(f"• Преподаватель: {profile['teacher']}")
            if profile.get("discipline"):
                items.append(f"• Дисциплина: {profile['discipline']}")
            if profile.get("variant"):
                items.append(f"• ВАРИАНТ ЗАДАНИЯ: Выполни строго для варианта №{profile['variant']}")
            if profile.get("antiplagiat_mode"):
                items.append("• РЕЖИМ АНТИПЛАГИАТ: Пиши введение, ход работы и выводы уникальным авторским языком, избегая шаблонных фраз, чтобы отчет легко проходил проверку на оригинальность 80%+.")
            
            if items:
                profile_details = "СОХРАНЕННЫЙ ПРОФИЛЬ И ПАРАМЕТРЫ СТУДЕНТА:\n" + "\n".join(items) + "\n\n"

        user_prompt = (
            f"{profile_details}"
            f"МЕТОДИЧЕСКИЕ УКАЗАНИЯ / ТЕКСТ ЛАБОРАТОРНОЙ РАБОТЫ:\n"
            f"{lab_text}\n\n"
            f"ДОПОЛНИТЕЛЬНЫЕ ПОЖЕЛАНИЯ СТУДЕНТА:\n"
            f"{user_instructions or 'Сделай качественный типовой отчет по ГОСТ с титульным листом, диаграммой, таблицами и ответами на контрольные вопросы в самом конце.'}\n\n"
            f"Напиши полный Python-код скрипта генерации result.docx."
        )
        logger.info("Отправка запроса на первичную генерацию кода в LLM...")
        raw_resp = await self._call_llm(SYSTEM_PROMPT_GENERATOR, user_prompt)
        return self._extract_code(raw_resp)

    async def edit_existing_code(self, old_code: str, edit_instructions: str) -> str:
        """Внесение правок в существующий скрипт по запросу пользователя."""
        user_prompt = (
            f"ПРЕДЫДУЩАЯ ВЕРСИЯ СКРИПТА:\n"
            f"```python\n{old_code}\n```\n\n"
            f"ТРЕБУЕМЫЕ ИЗМЕНЕНИЯ И ПРАВКИ:\n"
            f"{edit_instructions}\n\n"
            f"Верни обновленный и полностью работоспособный скрипт генерации result.docx целиком."
        )
        logger.info("Отправка запроса на модификацию скрипта в LLM...")
        raw_resp = await self._call_llm(SYSTEM_PROMPT_EDITOR, user_prompt)
        return self._extract_code(raw_resp)

    async def fix_broken_code(self, broken_code: str, error_message: str) -> str:
        """Автоматическое исправление скрипта, если при его выполнении возникло исключение."""
        user_prompt = (
            f"ИСХОДНЫЙ КОД С ОШИБКОЙ:\n"
            f"```python\n{broken_code}\n```\n\n"
            f"ЛОГ ОШИБКИ ВЫПОЛНЕНИЯ:\n"
            f"{error_message}\n\n"
            f"Проанализируй ошибку, исправь неверный код и верни исправленный скрипт целиком."
        )
        logger.info("Отправка запроса на авто-исправление кода в LLM...")
        raw_resp = await self._call_llm(SYSTEM_PROMPT_FIXER, user_prompt)
        return self._extract_code(raw_resp)


llm_service = LLMService()
