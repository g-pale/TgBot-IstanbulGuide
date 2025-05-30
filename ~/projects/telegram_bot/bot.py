import os
import sys
import logging
import tempfile
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from dotenv import load_dotenv
import requests
import re
from collections import defaultdict, deque

load_dotenv()  # Загружает переменные из .env в окружение

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token and OpenRouter API key
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not TOKEN:
    print("[ОШИБКА] Переменная окружения TELEGRAM_BOT_TOKEN не задана! Бот завершает работу.")
    sys.exit(1)
if not OPENROUTER_API_KEY:
    print("[ОШИБКА] Переменная окружения OPENROUTER_API_KEY не задана! Бот завершает работу.")
    sys.exit(1)

os.environ["OPENAI_API_KEY"] = OPENROUTER_API_KEY

# Глобальная инициализация OpenAI клиента
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": "your-site.com",  # Замените на ваш сайт
        "X-Title": "Telegram Bot"         # Любое название
    }
)

PROMPT_TEMPLATE = '''
Ты — экспертный Telegram-бот, который отвечает на запросы пользователя чётко и структурировано. 
Всегда используй следующую разметку:

– Краткая вводная фраза.

1. Название первого раздела

Текст первого раздела.

2. Название второго раздела

Текст второго раздела.

3. Название третьего раздела

Текст третьего раздела.

…и так далее.

При необходимости внутри раздела добавляй ненумерованные подзаголовки (раздели их пустыми строками).

В конце можно добавить совет в формате:
💡 Совет: …

Вопрос пользователя:
{question}

Ответ бота:
'''

OPENWEATHER_API_KEY = "79b333a6fa52cf366d5437b7ecff03c3"

# Глобальный словарь для истории сообщений (user_id -> deque)
user_histories = defaultdict(lambda: deque(maxlen=10))  # храним последние 10 сообщений

def chunk_text(text: str, max_len: int = 1000):
    """
    Разбивает текст на части длиной до max_len, стараясь разрезать по предложениям.
    """
    chunks = []
    current = ""
    for sentence in text.replace("\n", " ").split('. '):
        part = sentence + ('. ' if not sentence.endswith('.') else '')
        if len(current) + len(part) > max_len:
            if current:
                chunks.append(current.strip())
            current = part
        else:
            current += part
    if current:
        chunks.append(current.strip())
    return chunks

def city_locative(city):
    locative = {
        "Москва": "Москве",
        "Санкт-Петербург": "Санкт-Петербурге",
        "Екатеринбург": "Екатеринбурге",
        "Новосибирск": "Новосибирске",
        "Казань": "Казани",
        "Нижний Новгород": "Нижнем Новгороде",
        "Ростов-на-Дону": "Ростове-на-Дону",
        "Самара": "Самаре",
        "Омск": "Омске",
        "Челябинск": "Челябинске",
        "Уфа": "Уфе",
        "Красноярск": "Красноярске",
        "Воронеж": "Воронеже",
        "Пермь": "Перми",
        "Волгоград": "Волгограде",
        "Стамбул": "Стамбуле",
        "Istanbul": "Istanbul"
    }
    return locative.get(city, city)

def get_weather(city: str):
    url = f"https://api.openweathermap.org/data/2.5/weather"
    params = {
        "q": city,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "ru"
    }
    try:
        response = requests.get(url, params=params, timeout=5)
        data = response.json()
        logger.info(f"DEBUG: city={city}, response={data}")
        if data.get("cod") != 200:
            return None
        temp = data["main"]["temp"]
        desc = data["weather"][0]["description"]
        return f"Сейчас в {city_locative(city)} {temp}°C, {desc}."
    except Exception as e:
        return None

def get_weather_forecast(city: str, days: int = 3):
    api_key = OPENWEATHER_API_KEY
    # 1. Получаем координаты города
    geo_url = "http://api.openweathermap.org/geo/1.0/direct"
    geo_params = {"q": city, "limit": 1, "appid": api_key}
    geo_resp = requests.get(geo_url, params=geo_params, timeout=5)
    geo_data = geo_resp.json()
    if not geo_data:
        return None
    lat, lon = geo_data[0]["lat"], geo_data[0]["lon"]

    # 2. Получаем прогноз
    forecast_url = "https://api.openweathermap.org/data/2.5/forecast"
    forecast_params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": "metric",
        "lang": "ru"
    }
    resp = requests.get(forecast_url, params=forecast_params, timeout=5)
    data = resp.json()
    if "list" not in data:
        return None

    from datetime import datetime
    from collections import defaultdict

    days_data = defaultdict(list)
    for entry in data["list"]:
        date = datetime.fromtimestamp(entry["dt"]).strftime("%Y-%m-%d")
        days_data[date].append(entry)

    result = []
    for i, (date, entries) in enumerate(days_data.items()):
        if i >= days:
            break
        temps = [e["main"]["temp"] for e in entries]
        desc = entries[0]["weather"][0]["description"]
        avg_temp = sum(temps) / len(temps)
        result.append(f"{date}: {avg_temp:.1f}°C, {desc}")

    return "\n".join(result)

def normalize_city(city):
    city = city.strip().lower()
    if city in ["москва", "москве"]:
        return "Москва"
    if city in ["стамбул", "стамбуле"]:
        return "Стамбул"
    if city in ["istanbul"]:
        return "Istanbul"
    if city in ["санкт-петербург", "санкт-петербурге", "питер", "питере"]:
        return "Санкт-Петербург"
    if city in ["екатеринбург", "екатеринбурге"]:
        return "Екатеринбург"
    if city in ["новосибирск", "новосибирске"]:
        return "Новосибирск"
    if city in ["казань", "казани"]:
        return "Казань"
    if city in ["нижний новгород", "нижнем новгороде"]:
        return "Нижний Новгород"
    if city in ["ростов-на-дону", "ростове-на-дону"]:
        return "Ростов-на-Дону"
    if city in ["самара", "самаре"]:
        return "Самара"
    if city in ["омск", "омске"]:
        return "Омск"
    if city in ["челябинск", "челябинске"]:
        return "Челябинск"
    if city in ["уфа", "уфе"]:
        return "Уфа"
    if city in ["красноярск", "красноярске"]:
        return "Красноярск"
    if city in ["воронеж", "воронеже"]:
        return "Воронеж"
    if city in ["пермь", "перми"]:
        return "Пермь"
    if city in ["волгоград", "волгограде"]:
        return "Волгоград"
    # Для английских названий и остальных городов
    return city.title()

def extract_city(raw_city):
    # Если в строке есть 'в <город>', берём последнее слово после 'в'
    match = re.search(r'в\s+([а-яА-Яa-zA-ZёЁ\- ]+)', raw_city, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # Если строка состоит из нескольких слов, берём последнее
    words = raw_city.strip().split()
    if len(words) > 1:
        return words[-1]
    return raw_city.strip()

def format_gpt_answer(text):
    # Удалить все * кроме жирных пунктов
    text = re.sub(r'(?<!\*)\*+', '', text)
    # Добавить перенос строки после жирного пункта, если его нет
    text = re.sub(r'(\*\*\d+\. [^\n]*)', r'\1\n', text)
    # Разбить подпункты (например, 1.1 ...) на отдельные строки
    text = re.sub(r'(\d+\.\d+ )', r'\n\1', text)
    # Убрать лишние пробелы и пустые строки
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Убрать пробелы в начале строк
    text = re.sub(r'\n +', '\n', text)
    return text.strip()

def fix_markdown(text):
    # Закрыть незакрытые **
    if text.count('**') % 2 != 0:
        text += '**'
    # Удалить лишние одинарные * внутри текста (оставить только в начале подпунктов)
    # Теперь подпункты — только нумерация, * не должно быть
    lines = text.split('\n')
    fixed_lines = []
    for line in lines:
        # Не трогаем строки с жирным пунктом
        if re.match(r'^\*\*\d+\. ', line):
            fixed_lines.append(line)
        else:
            fixed_lines.append(line.replace('*', ''))
    return '\n'.join(fixed_lines)

def clean_answer(text):
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Жирный текст: **текст** → <b>текст</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    return text.strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    menu_keyboard = [
        [KeyboardButton("📄 Новый диалог"), KeyboardButton("ℹ️ Помощь")],
        [KeyboardButton("⚙️ Настройки"), KeyboardButton("📝 О боте")]
    ]
    reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"Привет, {user.first_name}! Я бот OpenRouterGPT.",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я понимаю команды:\n"
        "/start - Запустить бота\n"
        "/help - Показать справку\n"
        "Просто отправьте мне сообщение, и я отвечу текстом и голосом."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if text == "ℹ️ Помощь":
        await help_command(update, context)
        return
    elif text == "📝 О боте":
        await update.message.reply_text(
            "Я Telegram-бот, использующий OpenRouter для генерации ответов с помощью ИИ."
        )
        return
    elif text == "📄 Новый диалог":
        user_histories[user_id].clear()
        await update.message.reply_text(
            "Новый диалог начат! Можете задать свой вопрос."
        )
        return
    elif text == "⚙️ Настройки":
        await update.message.reply_text(
            "Настройки пока недоступны. В будущем здесь появятся дополнительные опции."
        )
        return
    # Улучшенное регулярное выражение для извлечения города
    match = re.search(r'(температура|погода|прогноз).*(в|по|для)\s*([а-яА-Яa-zA-Z\- ]+)', text, re.IGNORECASE)
    if match:
        city = normalize_city(extract_city(match.group(3)))
        # Проверка на прогноз на 3 дня
        if re.search(r'(на\s*3\s*дня|на\s*три\s*дня|прогноз)', text, re.IGNORECASE):
            forecast = get_weather_forecast(city, days=3)
            if forecast:
                await update.message.reply_text(f"Прогноз погоды в {city} на ближайшие 3 дня:\n{forecast}")
            else:
                await update.message.reply_text("Не удалось получить прогноз погоды. Проверьте название города.")
            return
        weather = get_weather(city)
        if weather:
            await update.message.reply_text(weather)
        else:
            await update.message.reply_text("Не удалось получить данные о погоде. Проверьте название города.")
        return
    # --- Новый подход: история + мягкий промпт ---
    user_question = text
    user_histories[user_id].append({"role": "user", "content": user_question})

    PROMPT_TEMPLATE = """
Ты — умный и краткий Telegram-бот. Отвечай на вопросы пользователей чётко, структурировано и по делу.

Если вопрос относится к прошлому диалогу — обязательно учитывай контекст.

Структурируй ответ в разделы **только если это уместно**, но не жертвуй фактами ради формы.

💡 В конце можешь добавить совет или интересный факт.
    """.strip()

    messages = [{"role": "system", "content": PROMPT_TEMPLATE}]
    messages.extend(user_histories[user_id])

    try:
        response = client.chat.completions.create(
            model="openai/gpt-3.5-turbo-0613",
            messages=messages,
            temperature=0.4,
            max_tokens=1024,
        )
        if not response.choices:
            logger.error(f"Пустой ответ от API: {response}")
            raise ValueError("GPT не вернул ответ")

        answer = response.choices[0].message.content.strip()

        # Сохраняем ответ тоже в историю
        user_histories[user_id].append({"role": "assistant", "content": answer})

        await update.message.reply_text(
            clean_answer(answer),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Ошибка при обращении к GPT: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Произошла ошибка при обработке запроса.")

def main() -> None:
    app = ApplicationBuilder()\
        .token(TOKEN)\
        .build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # <-- вот здесь сбрасываем pending updates
    app.run_polling(drop_pending_updates=True)



if __name__ == '__main__':
    main()

