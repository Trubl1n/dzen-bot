#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dzen News Bot - Telegram bot для парсинга новостей с Дзена
"""

import asyncio
import aiosqlite
import logging
import sys
import os
import difflib
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = os.getenv('BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
CHANNEL_ID = os.getenv('CHANNEL_ID')
MY_CHANNEL_LINK = os.getenv('MY_CHANNEL_LINK', 'https://t.me/krasnodarskiy_veter')
MY_CHANNEL_NAME = os.getenv('MY_CHANNEL_NAME', 'Краснодарский ветер')

DZEN_CHANNELS = [
    'https://dzen.ru/kommersant_kuban', 
    'https://dzen.ru/tvkrasnodar',
    'https://dzen.ru/novosti_kuban24'
]
# ================================================

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8', mode='a')
    ]
)
logger = logging.getLogger(__name__)

# Инициализация бота и Gemini
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={'api_version': 'v1beta'}
)

safety_settings = [
    genai_types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_NONE'),
    genai_types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_NONE'),
    genai_types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_NONE'),
    genai_types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_NONE'),
]

# ================= БАЗА ДАННЫХ =================
async def init_db():
    """Инициализация базы данных"""
    async with aiosqlite.connect('news.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS articles (
                url TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()
    logger.info("✅ База данных инициализирована")

async def is_duplicate(url, title):
    """Проверка новости на дубликат"""
    async with aiosqlite.connect('news.db') as db:
        # Проверка по URL
        cursor = await db.execute('SELECT title FROM articles WHERE url = ?', (url,))
        if await cursor.fetchone():
            return True
        
        if not title:
            return False
        
        # Проверка по заголовку (похожесть >85%)
        cursor = await db.execute('SELECT title FROM articles WHERE title IS NOT NULL AND status != "rejected"')
        rows = await cursor.fetchall()
        
        for row in rows:
            db_title = row[0]
            if not db_title:
                continue
            similarity = difflib.SequenceMatcher(None, title.lower(), db_title.lower()).ratio()
            if similarity > 0.85:
                logger.info(f"♻️ Дубликат: '{title[:50]}...' ≈ '{db_title[:50]}...'")
                return True
        return False

async def add_article(url, title, status='pending'):
    """Добавление статьи в базу"""
    async with aiosqlite.connect('news.db') as db:
        await db.execute(
            'INSERT OR REPLACE INTO articles (url, title, status) VALUES (?, ?, ?)',
            (url, title, status)
        )
        await db.commit()

# ================= ИИ ГЕНЕРАЦИЯ =================
async def generate_post_content(text_content):
    """Генерация поста через Gemini"""
    if not text_content or len(text_content.strip()) < 100:
        return "SKIP"
    
    prompt = (
        f"Ты — редактор телеграм-канала '{MY_CHANNEL_NAME}'. Создай короткий пост из новости.\n"
        f"Исходный текст: {text_content[:7000]}...\n\n"
        f"ТРЕБОВАНИЯ:\n"
        f"1. Заголовок в <b>жирном</b> формате + 1-2 эмодзи в начале.\n"
        f"2. Пустая строка после заголовка.\n"
        f"3. Краткая суть новости в 2-3 предложениях.\n"
        f"4. Если новость старая (даты старше 2 дней) — верни ТОЛЬКО слово: SKIP.\n"
        f"5. Если текст рекламный, бессмысленный или неинформативный — верни ТОЛЬКО: SKIP.\n"
        f"6. Не добавляй никаких пояснений, только готовый пост в формате HTML.\n"
        f"7. Максимальная длина поста — 800 символов."
    )
    
    try:
        response = await client.aio.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                safety_settings=safety_settings,
                temperature=0.3,
                max_output_tokens=500
            )
        )
        result = response.text.strip() if response.text else ""
        result = result.replace("```html", "").replace("```", "").strip()
        
        if result.upper() == "SKIP" or not result:
            return "SKIP"
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка Gemini: {e}")
        return "SKIP"

# ================= ПАРСЕР =================
async def parse_dzen_and_process():
    """Основная функция парсинга"""
    browser = None
    context = None
    page = None
    
    try:
        logger.info("♻️ Запуск браузера...")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--disable-gpu',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-sync',
                    '--disable-default-apps',
                    '--hide-scrollbars',
                    '--mute-audio',
                    '--js-flags="--max-old-space-size=128"'
                ]
            )
            
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            # Блокировка тяжёлых ресурсов
            await context.route("**/*.{png,jpg,jpeg,svg,mp4,webp,css,woff,woff2,gif,ico,ttf,eot}", lambda route: route.abort())
            await context.route("**/*font*", lambda route: route.abort())
            await context.route("**/analytics/*", lambda route: route.abort())
            
            page = await context.new_page()
            await page.set_extra_http_headers({'accept': 'text/html,application/xhtml+xml'})
            
            for channel_url in DZEN_CHANNELS:
                try:
                    logger.info(f"🔍 Канал: {channel_url}")
                    
                    await page.goto(channel_url, timeout=40000, wait_until="domcontentloaded")
                    await asyncio.sleep(1)
                    
                    # Поиск ссылок на статьи
                    link_elements = await page.query_selector_all('a[href*="/a/"]')
                    found_links = []
                    
                    for el in link_elements[:3]:
                        href = await el.get_attribute('href')
                        if not href:
                            continue
                        if not href.startswith('http'):
                            href = f"https://dzen.ru{href}"
                        clean_url = href.split('?')[0].split('#')[0]
                        if clean_url not in found_links:
                            found_links.append(clean_url)
                    
                    processed = 0
                    for article_url in found_links:
                        if processed >= 1:
                            break
                        
                        # Быстрая проверка дубля по URL
                        if await is_duplicate(article_url, None):
                            logger.info(f"⏭️ Пропущено (дубль по URL): {article_url[:60]}...")
                            continue
                        
                        logger.info(f"📄 Статья: {article_url}")
                        
                        try:
                            await page.goto(article_url, timeout=40000, wait_until="domcontentloaded")
                            await asyncio.sleep(0.5)
                            
                            # Проверка даты публикации
                            is_old = False
                            try:
                                date_meta = await page.locator('meta[property="article:published_time"]').first.get_attribute('content')
                                if date_meta:
                                    pub_date = datetime.strptime(date_meta.split('T')[0], "%Y-%m-%d")
                                    if (datetime.now() - pub_date).days > 2:
                                        is_old = True
                            except:
                                pass
                            
                            if is_old:
                                logger.info("⚠️ Старая новость (>2 дней), пропуск")
                                await add_article(article_url, "OLD", status='skipped')
                                continue
                            
                            # Получение заголовка
                            article_title = ""
                            try:
                                title_el = await page.locator('h1').first
                                if await title_el.count() > 0:
                                    article_title = (await title_el.inner_text()).strip()
                            except:
                                pass
                            
                            # Проверка дубля по заголовку
                            if await is_duplicate(article_url, article_title):
                                logger.info("⏭️ Пропущено (дубль по заголовку)")
                                await add_article(article_url, article_title, status='duplicate')
                                continue
                            
                            # Получение текста статьи
                            article_body = ""
                            try:
                                # Пробуем разные селекторы
                                for selector in ['article', 'main', '[role="main"]', 'body']:
                                    try:
                                        content = await page.locator(selector).first.inner_text()
                                        if content and len(content) > 200:
                                            article_body = content
                                            break
                                    except:
                                        continue
                            except:
                                pass
                            
                            article_body = article_body[:2000]  # Ограничение длины
                            
                            if not article_body.strip() or len(article_body.strip()) < 100:
                                logger.warning("⚠️ Пустой или слишком короткий текст")
                                continue
                            
                            # Генерация поста через ИИ
                            logger.info("🤖 Генерация поста через Gemini...")
                            post_text = await generate_post_content(article_body)
                            
                            if post_text == "SKIP" or not post_text.strip():
                                logger.info("⏭️ Пропущено (не подошло по критериям)")
                                await add_article(article_url, article_title, status='rejected')
                                continue
                            
                            # Отправка на модерацию
                            await send_to_admin_approval(post_text, article_url, article_title)
                            await add_article(article_url, article_title, status='pending_approval')
                            processed += 1
                            logger.info("✅ Пост отправлен на модерацию")
                            
                        except Exception as e:
                            logger.error(f"❌ Ошибка обработки статьи: {e}")
                            continue
                            
                except Exception as e:
                    logger.error(f"❌ Ошибка канала {channel_url}: {e}")
                    continue
                    
    except Exception as e:
        logger.error(f"💥 Критическая ошибка парсера: {e}", exc_info=True)
        
    finally:
        # Гарантированная очистка ресурсов
        try:
            if page:
                await page.close()
            if context:
                await context.close()
            if browser:
                await browser.close()
                logger.info("🔒 Браузер закрыт")
        except Exception as e:
            logger.error(f"⚠️ Ошибка при закрытии браузера: {e}")

# ================= БОТ =================
async def send_to_admin_approval(post_text, original_link, title):
    """Отправка поста админу на утверждение"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ В канал", callback_data=f"approve|{original_link}")
    builder.button(text="❌ Отклонить", callback_data=f"reject|{original_link}")
    builder.adjust(2)
    
    preview = post_text[:350] + "..." if len(post_text) > 350 else post_text
    admin_text = f"{preview}\n\n<i>🔗 Источник: {original_link}</i>"
    
    try:
        await bot.send_message(
            ADMIN_ID, 
            admin_text, 
            reply_markup=builder.as_markup(), 
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        logger.info("📤 Отправлено админу на модерацию")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки админу: {e}")

@dp.callback_query(lambda c: c.data.startswith('approve|') or c.data.startswith('reject|'))
async def handle_buttons(callback: types.CallbackQuery):
    """Обработка кнопок модерации"""
    action, url = callback.data.split('|', 1)
    
    try:
        if action == 'approve':
            content = callback.message.html_text
            if "----------" in content:
                clean_post = content.split("----------")[0].strip()
            elif "🔗 Источник:" in content:
                clean_post = content.split("🔗 Источник:")[0].strip()
            else:
                clean_post = content
            
            footer = f"\n\n<a href='{MY_CHANNEL_LINK}'>📬 {MY_CHANNEL_NAME} | Подписаться</a>"
            
            await bot.send_message(
                CHANNEL_ID, 
                f"{clean_post}{footer}", 
                parse_mode="HTML", 
                disable_web_page_preview=True
            )
            await callback.message.edit_text(f"{clean_post}\n\n✅ <b>Опубликовано в канал!</b>", parse_mode="HTML")
            await add_article(url, "PUBLISHED", status='published')
            logger.info(f"✅ Опубликовано: {url}")
            
        elif action == 'reject':
            await callback.message.edit_text("❌ <b>Отклонено</b>", parse_mode="HTML")
            await add_article(url, "REJECTED", status='rejected')
            logger.info(f"❌ Отклонено: {url}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка обработки кнопки: {e}")
        await callback.answer("Ошибка обработки", show_alert=True)
    
    await callback.answer()

# ================= ВЕБ-СЕРВЕР (health check) =================
from aiohttp import web

async def handle_health(request):
    return web.Response(text="✅ Bot is running", content_type='text/plain')

async def start_server():
    app = web.Application()
    app.router.add_get('/health', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    logger.info("🌐 Health-check сервер запущен на порту 10000")

# ================= SCHEDULER =================
async def scheduler():
    """Периодический запуск парсера"""
    while True:
        try:
            logger.info("🔄 Запуск цикла парсинга...")
            await parse_dzen_and_process()
            logger.info("⏳ Следующий запуск через 20 минут")
        except Exception as e:
            logger.error(f"💥 Ошибка в scheduler: {e}", exc_info=True)
        
        await asyncio.sleep(1200)  # 20 минут

# ================= MAIN =================
async def main():
    """Точка входа"""
    logger.info("🚀 Запуск бота...")
    
    await init_db()
    await start_server()
    
    # Запуск планировщика в фоне
    asyncio.create_task(scheduler())
    
    # Запуск поллинга бота
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🤖 Бот запущен и готов к работе")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"💥 Фатальная ошибка: {e}", exc_info=True)
    finally:
        # Корректное завершение
        try:
            asyncio.run(bot.close())
        except:
            pass
