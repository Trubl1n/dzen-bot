import asyncio
import aiosqlite
import logging
import sys
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright
from aiohttp import web

# --- ИМПОРТ БИБЛИОТЕКИ ---
from google import genai
from google.genai import types as genai_types

# ================= КОНФИГУРАЦИЯ (ВСТАВЬТЕ СВОИ ДАННЫЕ) =================

BOT_TOKEN = '8085313764:AAGivK9Wsp4bWIrZUdTlJWGefJRAUzqZnF4'
GEMINI_API_KEY = 'AIzaSyAa3rAK50OMQD3TwscVzWYfPTBupW0cX7o' # <-- Вставьте ключ, который вы создали
ADMIN_ID = 858396700             # Ваш ID
CHANNEL_ID = '-1003634910863'    # ID канала

# ДАННЫЕ ДЛЯ ПОДПИСИ
MY_CHANNEL_LINK = "https://t.me/krasnodarskiy_veter" 
MY_CHANNEL_NAME = "Краснодарский ветер"

DZEN_CHANNELS = [
    'https://dzen.ru/kommersant_kuban', 
    'https://dzen.ru/tvkrasnodar',
    'https://dzen.ru/novosti_kuban24'
]

# =======================================================================

# Фейковый веб-сервер для Render
async def handle(request):
    return web.Response(text="Bot is alive")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render требует слушать порт, который он выдаст в переменной окружения PORT, или 10000
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()


logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- НАСТРОЙКА КЛИЕНТА GEMINI ---
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={'api_version': 'v1beta'} # Для версий 2.5 и 3 нужна бета
)

# Настройки безопасности (разрешаем всё, чтобы не блочил новости про ДТП)
safety_settings = [
    genai_types.SafetySetting(
        category='HARM_CATEGORY_HATE_SPEECH',
        threshold='BLOCK_NONE'
    ),
    genai_types.SafetySetting(
        category='HARM_CATEGORY_DANGEROUS_CONTENT',
        threshold='BLOCK_NONE'
    ),
    genai_types.SafetySetting(
        category='HARM_CATEGORY_HARASSMENT',
        threshold='BLOCK_NONE'
    ),
    genai_types.SafetySetting(
        category='HARM_CATEGORY_SEXUALLY_EXPLICIT',
        threshold='BLOCK_NONE'
    ),
]

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect('news.db') as db:
        await db.execute('CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, title TEXT, status TEXT)')
        await db.commit()

async def url_exists(url):
    async with aiosqlite.connect('news.db') as db:
        cursor = await db.execute('SELECT 1 FROM articles WHERE url = ?', (url,))
        return await cursor.fetchone() is not None

async def add_article(url, title, status='pending'):
    async with aiosqlite.connect('news.db') as db:
        await db.execute('INSERT OR IGNORE INTO articles (url, title, status) VALUES (?, ?, ?)', (url, title, status))
        await db.commit()

# --- ИИ: ГЕНЕРАЦИЯ ПОСТА ---
async def generate_post_content(text_content):
    """Просит ИИ переписать новость"""
    
    prompt = (
        f"Ты — редактор Telegram-канала '{MY_CHANNEL_NAME}'. Твоя задача — сделать из текста новости короткий, красивый пост.\n"
        f"Исходный текст: {text_content[:8000]}...\n\n" 
        f"ТРЕБОВАНИЯ К ФОРМАТУ:\n"
        f"1. Первая строка: Кликбейтный заголовок (но правдивый), выделенный жирным шрифтом (тэги <b> и </b>). Добавь 1-2 эмодзи в конце заголовка.\n"
        f"2. Сделай пустую строку после заголовка.\n"
        f"3. Далее напиши суть новости (саммари) в 2-3 предложениях. Убери лишнюю воду.\n"
        f"4. Если новость ОЧЕНЬ скучная, старая или рекламная, верни просто слово 'SKIP'.\n"
        f"5. НЕ пиши никаких 'Здравствуй', 'Вот пост'. Сразу выдавай готовый текст."
    )

    try:
        # ИСПОЛЬЗУЕМ GEMINI 2.5 FLASH (Она есть в вашем списке)
        response = await client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                safety_settings=safety_settings
            )
        )
        
        result = response.text.strip()
        result = result.replace("```html", "").replace("```", "")
        return result

    except Exception as e:
        logging.error(f"AI Error: {e}")
        return "SKIP"

# --- ПАРСЕР ---
async def parse_dzen_and_process():
    logging.info("Ищу новости...")
    
    async with async_playwright() as p:
        # ЗАПУСК БРАУЗЕРА С ОПТИМИЗАЦИЕЙ
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--single-process',
                '--disable-gpu'
            ]
        )
        
        # !!! ВАЖНО: Блокируем картинки, видео и шрифты, чтобы ускорить загрузку
        context = await browser.new_context()
        await context.route("**/*.{png,jpg,jpeg,svg,mp4,webp,css,woff,woff2}", lambda route: route.abort())
        
        page = await context.new_page()
        
        for url in DZEN_CHANNELS:
            try:
                # !!! Увеличиваем таймаут до 90 секунд (было 60000 или по умолчанию 30000)
                await page.goto(url, timeout=90000, wait_until="domcontentloaded")
                await asyncio.sleep(5) # Даем чуть больше времени на прогрузку скриптов
                
                link_elements = await page.query_selector_all('a[href*="/a/"]')
                
                found_links = []
                for el in link_elements[:3]: 
                    href = await el.get_attribute('href')
                    if not href: continue
                    if not href.startswith('http'): href = f"https://dzen.ru{href}"
                    href = href.split('?')[0]
                    found_links.append(href)

                for article_url in found_links:
                    if await url_exists(article_url):
                        continue 
                    
                    logging.info(f"Читаю статью: {article_url}")
                    
                    try:
                        # !!! ВОТ ЗДЕСЬ БЫЛА ОШИБКА. Увеличиваем время ожидания статьи до 2 минут (120000 мс)
                        await page.goto(article_url, timeout=120000, wait_until="domcontentloaded")
                        await asyncio.sleep(3)
                        
                        article_body = await page.inner_text('article')
                        
                        if not article_body:
                             article_body = await page.inner_text('body')

                        if not article_body or len(article_body) < 100:
                            logging.warning("Не удалось извлечь текст статьи")
                            await add_article(article_url, "Error parsing", status='error')
                            continue

                        post_text = await generate_post_content(article_body)
                        
                        if post_text == "SKIP":
                            logging.info("Новость пропущена ИИ (SKIP)")
                            await add_article(article_url, "Skipped by AI", status='rejected')
                            continue

                        await send_to_admin_approval(post_text, article_url)
                        await add_article(article_url, "Processed", status='review')
                        
                    except Exception as e:
                        # Если даже за 2 минуты не успел - пишем ошибку, но не падаем
                        logging.error(f"Ошибка внутри статьи {article_url}: {e}")

            except Exception as e:
                logging.error(f"Ошибка канала {url}: {e}")

        await browser.close()

# --- БОТ: ОТПРАВКА ---
async def send_to_admin_approval(post_text, original_link):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ В канал", callback_data="approve")
    builder.button(text="❌ Удалить", callback_data="reject")
    builder.adjust(2)

    admin_text = f"{post_text}\n\n----------\n<i>Источник: {original_link}</i>"
    
    if len(admin_text) > 4096:
        admin_text = admin_text[:4000] + "..."

    await bot.send_message(ADMIN_ID, admin_text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)

@dp.callback_query()
async def handle_buttons(callback: types.CallbackQuery):
    action = callback.data
    content = callback.message.html_text 
    
    if "----------" in content:
        clean_post = content.split("----------")[0].strip()
    else:
        clean_post = content

    if action == "approve":
        footer = f"<a href='{MY_CHANNEL_LINK}'>{MY_CHANNEL_NAME} | Подписаться</a>"
        final_message = f"{clean_post}\n\n{footer}"

        try:
            await bot.send_message(CHANNEL_ID, final_message, parse_mode="HTML", disable_web_page_preview=True)
            await callback.message.edit_text(f"{clean_post}\n\n✅ <b>Опубликовано!</b>", parse_mode="HTML")
        except Exception as e:
             await callback.message.edit_text(f"Ошибка: {e}")

    elif action == "reject":
        await callback.message.delete()
    
    await callback.answer()

async def scheduler():
    while True:
        await parse_dzen_and_process()
        logging.info("Цикл завершен. Жду 30 минут...")
        await asyncio.sleep(1800)

async def main():
    await init_db()
    # Запускаем фейковый сервер
    await start_web_server()
    # Запускаем планировщик и бота
    asyncio.create_task(scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())
