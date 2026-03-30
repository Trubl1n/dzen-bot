import asyncio
import aiosqlite
import logging
import sys
import difflib # <-- Библиотека для сравнения текста
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright

# --- ИМПОРТ GEMINI ---
from google import genai
from google.genai import types as genai_types

# ================= КОНФИГУРАЦИЯ =================

BOT_TOKEN = os.getenv('BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID'))

MY_CHANNEL_LINK = "https://t.me/krasnodarskiy_veter" 
MY_CHANNEL_NAME = "Краснодарский ветер"

DZEN_CHANNELS = [
    'https://dzen.ru/kommersant_kuban', 
    'https://dzen.ru/tvkrasnodar',
    'https://dzen.ru/novosti_kuban24'
]

# ================================================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={'api_version': 'v1beta'}
)

safety_settings = [
    genai_types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_NONE'),
    genai_types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_NONE'),
]

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect('news.db') as db:
        # Добавили поле title_hash для проверки заголовков
        await db.execute('CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, title TEXT, status TEXT)')
        await db.commit()

# --- ПРОВЕРКА НА ДУБЛИКАТЫ (САМОЕ ВАЖНОЕ) ---
async def is_duplicate(url, title):
    async with aiosqlite.connect('news.db') as db:
        # 1. Проверка по URL (прямое совпадение)
        cursor = await db.execute('SELECT title FROM articles WHERE url = ?', (url,))
        if await cursor.fetchone():
            return True

        # 2. Проверка по Заголовку (на случай, если ссылка изменилась)
        # Если заголовок пустой, пропускаем проверку
        if not title:
            return False
            
        # Достаем все заголовки за последние 24 часа (или все, если база маленькая)
        cursor = await db.execute('SELECT title FROM articles WHERE title IS NOT NULL')
        rows = await cursor.fetchall()
        
        for row in rows:
            db_title = row[0]
            if not db_title: continue
            
            # Сравниваем похожесть строк (от 0 до 1)
            # Если заголовки похожи на 85% и более — это одна и та же новость
            similarity = difflib.SequenceMatcher(None, title.lower(), db_title.lower()).ratio()
            if similarity > 0.85:
                logging.info(f"♻️ Обнаружен дубликат по заголовку: '{title}' совпадает с '{db_title}'")
                return True
                
        return False

async def add_article(url, title, status='pending'):
    async with aiosqlite.connect('news.db') as db:
        await db.execute('INSERT OR IGNORE INTO articles (url, title, status) VALUES (?, ?, ?)', (url, title, status))
        await db.commit()

# --- ИИ ГЕНЕРАЦИЯ ---
async def generate_post_content(text_content):
    prompt = (
        f"Ты — редактор канала '{MY_CHANNEL_NAME}'. Сделай короткий пост.\n"
        f"Текст: {text_content[:8000]}...\n\n" 
        f"ТРЕБОВАНИЯ:\n"
        f"1. Заголовок жирным (<b>текст</b>) + эмодзи.\n"
        f"2. Пустая строка.\n"
        f"3. Саммари (суть) в 2-3 предложениях.\n"
        f"4. Если новость старая (в тексте есть даты прошлого года или старые месяцы) -> верни 'SKIP'.\n"
        f"5. Если новость рекламная или скучная -> верни 'SKIP'."
    )
    try:
        response = await client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(safety_settings=safety_settings)
        )
        return response.text.strip().replace("```html", "").replace("```", "")
    except Exception as e:
        return "SKIP"

# --- ПАРСЕР ---
async def parse_dzen_and_process():
    logging.info("♻️ Запуск браузера...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas', '--no-first-run', '--no-zygote',
                '--single-process', '--disable-gpu', '--js-flags="--max-old-space-size=256"'
            ]
        )
        
        context = await browser.new_context()
        await context.route("**/*.{png,jpg,jpeg,svg,mp4,webp,css,woff,woff2,gif}", lambda route: route.abort())
        page = await context.new_page()
        
        for url in DZEN_CHANNELS:
            try:
                logging.info(f"🔍 Смотрю канал: {url}")
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                
                link_elements = await page.query_selector_all('a[href*="/a/"]')
                found_links = []
                for el in link_elements[:5]: 
                    href = await el.get_attribute('href')
                    if not href: continue
                    if not href.startswith('http'): href = f"https://dzen.ru{href}"
                    found_links.append(href.split('?')[0])

                processed_count = 0 

                for article_url in found_links:
                    if processed_count >= 1: break

                    # Предварительная проверка (если ссылка есть в базе, даже не открываем)
                    # Заголовок пока не знаем, передаем None
                    if await is_duplicate(article_url, None): 
                        continue

                    logging.info(f"📄 Проверяю статью: {article_url}")
                    
                    try:
                        await page.goto(article_url, timeout=60000, wait_until="domcontentloaded")
                        
                        # 1. ПРОВЕРКА ДАТЫ
                        try:
                            date_meta = await page.locator('meta[property="article:published_time"]').get_attribute('content')
                            if date_meta:
                                pub_date = datetime.strptime(date_meta.split('T')[0], "%Y-%m-%d")
                                if (datetime.now() - pub_date).days > 1:
                                    logging.info(f"⚠️ Старая новость. Скип.")
                                    # Пишем в базу, что это старье
                                    await add_article(article_url, "Old News", status='skipped')
                                    continue
                        except: pass

                        # 2. ПОЛУЧЕНИЕ ЗАГОЛОВКА ДЛЯ ПРОВЕРКИ ДУБЛЕЙ
                        # Пытаемся найти h1
                        article_title = ""
                        try:
                            article_title = await page.inner_text('h1')
                        except: pass
                        
                        # ВТОРАЯ ПРОВЕРКА НА ДУБЛИ (уже по заголовку)
                        if await is_duplicate(article_url, article_title):
                            logging.info("⚠️ Дубль по заголовку. Скип.")
                            await add_article(article_url, article_title, status='duplicate')
                            continue

                        # Получение текста
                        article_body = await page.inner_text('article')
                        if not article_body: article_body = await page.inner_text('body')

                        post_text = await generate_post_content(article_body)
                        
                        if post_text != "SKIP":
                            await send_to_admin_approval(post_text, article_url, article_title)
                            # Сразу записываем в базу, чтобы при рестарте не забыл
                            await add_article(article_url, article_title, status='review')
                            processed_count += 1
                        else:
                            await add_article(article_url, article_title, status='rejected')
                            
                    except Exception as e:
                        logging.error(f"Ошибка статьи: {e}")

            except Exception as e:
                logging.error(f"Ошибка канала: {e}")

        await page.close()
        await context.close()
        await browser.close()

# --- БОТ: ОТПРАВКА ---
async def send_to_admin_approval(post_text, original_link, title):
    builder = InlineKeyboardBuilder()
    # Добавляем callback c действием reject_db, чтобы точно пометить в базе
    builder.button(text="✅ В канал", callback_data="approve")
    builder.button(text="❌ Удалить", callback_data="reject") 
    builder.adjust(2)
    
    admin_text = f"{post_text}\n\n----------\n<i>Источник: {original_link}</i>"
    if len(admin_text) > 4096: admin_text = admin_text[:4000] + "..."
    
    await bot.send_message(ADMIN_ID, admin_text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)

@dp.callback_query()
async def handle_buttons(callback: types.CallbackQuery):
    action = callback.data
    content = callback.message.html_text 
    if "----------" in content: clean_post = content.split("----------")[0].strip()
    else: clean_post = content

    if action == "approve":
        footer = f"<a href='{MY_CHANNEL_LINK}'>{MY_CHANNEL_NAME} | Подписаться</a>"
        try:
            await bot.send_message(CHANNEL_ID, f"{clean_post}\n\n{footer}", parse_mode="HTML", disable_web_page_preview=True)
            await callback.message.edit_text(f"{clean_post}\n\n✅ <b>Опубликовано!</b>", parse_mode="HTML")
            # Статус в базе уже 'review' или 'processed', все ок
        except Exception as e: await callback.message.edit_text(f"Ошибка: {e}")

    elif action == "reject":
        # Просто удаляем сообщение, в базе она уже есть (мы добавили её при парсинге)
        # И второй раз бот её не пришлет, потому что она есть в базе
        await callback.message.delete()
    
    await callback.answer()

# --- ВЕБ-СЕРВЕР ---
from aiohttp import web
async def handle(request): return web.Response(text="Bot is running")
async def start_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 10000).start()

async def scheduler():
    while True:
        await parse_dzen_and_process()
        logging.info("Жду 20 минут...")
        await asyncio.sleep(1200)

async def main():
    await init_db(); await start_server()
    asyncio.create_task(scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
