import aiohttp
import asyncio
from typing import Optional, List

# Используем облачный API Mistral
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MODEL_NAME = "mistral-large-latest"


# --- Вспомогательная функция запроса ---
async def _call_mistral(messages: List[dict], api_key: str) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.7
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(MISTRAL_API_URL, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("choices", [{}])[0].get("message", {}).get("content", "Error")
                else:
                    error_text = await response.text()
                    return f"API Error {response.status}: {error_text}"
    except Exception as e:
        return f"Connection Error: {str(e)}"


# --- 1. Стандартный Ревью (с контекстом) ---
async def get_code_review_async(code: str, context: str = "", api_key: str = "") -> str:
    prompt = f"""
    Ты опытный Python-разработчик и security-аудитор.
    ПРАВИЛА ПРОЕКТА (Из .clinerules):
    {context}

    Код:
    ```python
    {code}
    ```
    Ответ в формате Markdown:
    1. Общая оценка.
    2. Найденные баги/риски.
    3. Рекомендации по рефакторингу.
    """
    return await _call_mistral([{"role": "user", "content": prompt}], api_key)


# --- 2. Генерация .clinerules (Долгосрочная память) ---
async def generate_clinerules_async(project_files: List[str], requirements_txt: str, api_key: str) -> str:
    files_str = "\n".join(project_files[:30])

    prompt = f"""
    Ты архитектор ПО. Проанализируй структуру проекта и зависимости.

    Файловая структура (первые 30 файлов):
    {files_str}

    Зависимости (requirements.txt):
    {requirements_txt}

    Создай файл '.clinerules'. В опиши:
    1. Используемые фреймворки (Django, FastAPI, Flask, версия).
    2. Архитектурные паттерны (MVC, MVVM, Microservices).
    3. Стандарты кодинга (type hints, docstrings, PEP8).
    4. Соглашения об именовании переменных и классов.

    Текст должен быть кратким и строгим инструкциям для AI-агента.
    """
    return await _call_mistral([{"role": "user", "content": prompt}], api_key)


# --- 3. Миграция кода (Python 3.11+, Django 4+) ---
async def migrate_code_async(code: str, current_stack: str, api_key: str) -> str:
    prompt = f"""
    Ты эксперт по современному Python (2025-2026 год).
    Текущий стек проекта: {current_stack}.

    Перепиши этот код, используя лучшие практики для Python 3.11+ и актуальных версий фреймворков.

    Требования:
    1. Используй `match/case` вместо сложных if-else where applicable.
    2. Добавь полную типизацию (typing) для аргументов и возврата.
    3. Убери устаревшие импорты (например, django.utils.six, старые werkzeug).
    4. Используй асинхронность (async/await), если паттерн позволяет.
    5. Используй f-strings.

    Старый код:
    ```python
    {code}
    ```

    Верни ТОЛЬКО новый код в блоке ```python ```. Без лишних слов.
    """
    return await _call_mistral([{"role": "user", "content": prompt}], api_key)


# --- 4. Генерация Тестов (Edge Cases) ---
async def generate_tests_async(code: str, api_key: str, context: str = "") -> str:
    """Параметр api_key теперь стоит ПЕРЕД context"""
    prompt = f"""
    Ты QA-инженер высокого уровня. Напиши модульные (Unit) и интеграционные тесты на pytest для этого кода.

    КОНТЕКСТ ПРОЕКТА:
    {context}

    КОД:
    ```python
    {code}
    ```

    Требования к тестам:
    1. Используй pytest fixtures.
    2. Покрой позитивные и негативные сценарии.
    3. ОБЯЗАТЕЛЬНО протестируй крайние случаи (edge cases): None, пустые списки [], деление на ноль, некорректные типы, слишком большие числа.
    4. Используй unittest.mock для внешних зависимостей.
    5. Используй параметризацию (parametrize) для проверки множества входных данных.

    Верни полный код тестов в блоке ```python ```.
    """
    return await _call_mistral([{"role": "user", "content": prompt}], api_key)


# --- 5. Скаффолдинг (Создание каркаса) ---
async def scaffold_app_async(description: str, api_key: str, stack: str = "FastAPI") -> str:
    prompt = f"""
    Ты Senior Backend разработчик. Создай каркас {stack} приложения по описанию.

    Описание:
    {description}

    Структура ответа (Markdown):
    1. `models.py` - SQLAlchemy / Pydantic модели (с типами).
    2. `main.py` или `views.py` - CRUD эндпоинты (с docstrings).
    3. `schemas.py` - Pydantic схемы (для валидации).

    Используй Python 3.11+, type hints, docstrings. Код должен быть готов к копированию.
    """
    return await _call_mistral([{"role": "user", "content": prompt}], api_key)