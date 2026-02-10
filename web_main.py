import asyncio
import os
import logging
from aiohttp import web

# ========== СОВМЕЩЕНИЕ БОТА И ВЕБ-СЕРВЕРА ==========
# Импортируем ваш основной код бота
# Убедитесь, что main.py находится в той же папке
from main import main as run_bot

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создаем веб-приложение
app = web.Application()

# Маршрут для проверки работоспособности (UptimeRobot будет пинговать этот URL)
async def health_check(request):
    return web.Response(text="Bot is running!")

# Маршрут для главной страницы
async def index(request):
    return web.Response(text="Telegram Spam Bot is running on Render!")

# Настраиваем маршруты
app.router.add_get('/', index)
app.router.add_get('/health', health_check)

# Запуск бота в фоне
async def start_background_tasks(app):
    # Запускаем Telegram бота в фоновой задаче
    app['bot_task'] = asyncio.create_task(run_bot())
    logger.info("Telegram bot started in background")

async def cleanup_background_tasks(app):
    # Останавливаем бота при завершении
    app['bot_task'].cancel()
    await app['bot_task']
    logger.info("Telegram bot stopped")

# Подключаем обработчики запуска и остановки
app.on_startup.append(start_background_tasks)
app.on_shutdown.append(cleanup_background_tasks)

# Функция для запуска веб-сервера
def start_web_server():
    port = int(os.getenv("PORT", 8080))  # Render предоставляет PORT
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == '__main__':
    start_web_server()