import asyncio
import aiosqlite
import logging
import sys
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright

# --- ИМПОРТ GEMINI ---
from google import genai
from google.genai import types as genai_types

# ================= КОНФИГУРАЦИЯ =================

BOT_TOKEN = '8085313764:AAGivK9Wsp4bWIrZUdTlJWGefJRAUzqZnF4'
GEMINI_API_KEY = 'AIzaSyAa3rAK50OMQD3TwscVzWYfPTBupW0cX7o' 
ADMIN_ID = 858396700
CHANNEL_ID = '-1003634910863'

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

# --- ИИ ГЕНЕРАЦИЯ ---
async def generate_post_content(text_content):
    prompt = (
        f"Ты — редактор канала '{MY_CHANNEL_NAME}'. Сделай короткий пост.\n"
        f"Текст: {text_content[:8000]}...\n\n" 
        f"ТРЕБОВАНИЯ:\n"
        f"1. Заголовок жирным (<b>текст</b>) + эмодзи.\n"
        f"2. Пустая строка.\n"
        f"3. Саммари (суть) в 2-3 предложениях.\n"
        f"4. Если новость старая, скучная или реклама -> верни 'SKIP'."
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

# --- ПАРСЕР С ФИЛЬТРОМ ДАТ ---
async def parse_dzen_and_process():
    logging.info("♻️ Запуск браузера...")
    
    async with async_playwright() as p:
        # ЗАПУСК В РЕЖИМЕ ЖЕСТКОЙ ЭКОНОМИИ (Для Render Free)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas', '--no-first-run', '--no-zygote',
                '--single-process', '--disable-gpu', '--js-flags="--max-old-space-size=256"'
            ]
        )
        
        context = await browser.new_context()
        # Блокируем всё лишнее для скорости
        await context.route("**/*.{png,jpg,jpeg,svg,mp4,webp,css,woff,woff2,gif}", lambda route: route.abort())
        page = await context.new_page()
        
        for url in DZEN_CHANNELS:
            try:
                logging.info(f"🔍 Смотрю канал: {url}")
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                
                # Ищем ссылки (берем больше, топ-5, чтобы пропустить закрепы)
                link_elements = await page.query_selector_all('a[href*="/a/"]')
                found_links = []
                for el in link_elements[:5]: 
                    href = await el.get_attribute('href')
                    if not href: continue
                    if not href.startswith('http'): href = f"https://dzen.ru{href}"
                    found_links.append(href.split('?')[0])

                # Счетчик свежих новостей для этого канала
                processed_count = 0 

                for article_url in found_links:
                    # Если уже обрабатывали - пропускаем
                    if await url_exists(article_url): continue 
                    
                    # Чтобы не перегрузить сервер, берем только 1-2 новости за раз
                    if processed_count >= 1: break

                    logging.info(f"📄 Проверяю статью: {article_url}")
                    
                    try:
                        await page.goto(article_url, timeout=60000, wait_until="domcontentloaded")
                        
                        # --- ПРОВЕРКА ДАТЫ ---
                        try:
                            # Ищем мета-тег с датой публикации
                            date_meta = await page.locator('meta[property="article:published_time"]').get_attribute('content')
                            # Формат обычно: 2025-12-22T10:00:00+03:00
                            if date_meta:
                                pub_date_str = date_meta.split('T')[0] # Берем только дату 2025-12-22
                                pub_date = datetime.strptime(pub_date_str, "%Y-%m-%d")
                                
                                # Если новости больше 2 дней - СКИПАЕМ
                                if (datetime.now() - pub_date).days > 1:
                                    logging.info(f"⚠️ Старая новость ({pub_date_str}). Пропускаю.")
                                    # Записываем в базу как processed, чтобы больше не открывать
                                    await add_article(article_url, "Old News", status='skipped')
                                    continue
                        except Exception as date_e:
                            logging.warning(f"Не нашел дату, пробую обработать так: {date_e}")

                        # --- ПОЛУЧЕНИЕ ТЕКСТА ---
                        article_body = await page.inner_text('article')
                        if not article_body: article_body = await page.inner_text('body')

                        post_text = await generate_post_content(article_body)
                        
                        if post_text != "SKIP":
                            await send_to_admin_approval(post_text, article_url)
                            await add_article(article_url, "Processed", status='review')
                            processed_count += 1 # Увеличиваем счетчик обработанных
                        else:
                            await add_article(article_url, "Skipped", status='rejected')
                            
                    except Exception as e:
                        logging.error(f"Ошибка статьи: {e}")

            except Exception as e:
                logging.error(f"Ошибка канала: {e}")

        await page.close()
        await context.close()
        await browser.close()
        logging.info("✅ Цикл завершен")

# --- БОТ: ОТПРАВКА ---
async def send_to_admin_approval(post_text, original_link):
    builder = InlineKeyboardBuilder()
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
        except Exception as e: await callback.message.edit_text(f"Ошибка: {e}")
    elif action == "reject": await callback.message.delete()
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
        logging.info("Жду 20 минут...") # Даем серверу остыть
        await asyncio.sleep(1200)

async def main():
    await init_db(); await start_server()
    asyncio.create_task(scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
