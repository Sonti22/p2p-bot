"""HTTP health-check endpoint для мониторинга бота (Grafana, UptimeRobot, etc).

Запуск: python health_http.py --port 8080
Требует: aiohttp (уже в requirements.txt)

Эндпоинты:
- GET /health — живой/мёртвый (200/503)
- GET /status — полная сводка (как /status в Telegram, но JSON)
- GET /metrics — Prometheus-метрики (опционально, через переменную PROMETRICS=1)

Переменные окружения:
- HEALTH_CHECK_PORT (по умолчанию: 8080)
- HEALTH_CHECK_PASSWORD — если задан, требует ?token=xxx в URL
- PROMETRICS — если "1", включает /metrics

Пример запуска в Docker:
  docker run -d --name p2p-health \
    -p 8080:8080 \
    -e HEALTH_CHECK_PASSWORD=mypassword \
    p2p-bot:latest python health_http.py

Пример curl:
  curl http://localhost:8080/health
  curl "http://localhost:8080/status?token=mypassword"
"""
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import aiohttp

# Глобальное состояние — читается из .dev_status.json launcher'ом
DEV_STATUS_PATH = "data/.dev_status.json"
LAST_GOOD_PATH = "data/.last_good"

logger = logging.getLogger(__name__)

# Хранилище метрик (заполняется ботом или health_http.py самостоятельно)
_metrics_store = {
    "uptime": None,           # float: время запуска
    "last_scan": None,        # float: unix timestamp последнего скана
    "last_scan_duration": None,  # float: длительность последнего скана (сек)
    "scan_count": 0,          # int: количество сканов
    "scan_errors": {},        # dict: venue -> last_error_timestamp
    "active_connections": 0,  # int: текущие подключения
    "version": "unknown",     # str: версия из launcher
    "commit_hash": None,      # str: последний коммит
    "is_update_pending": False,  # bool: есть ли обновление
    "paper_cycles": 0,        # int: циклы сухого прогона
    "paper_balance": 0,       # Decimal: баланс сухого прогона
}


def get_status() -> dict:
    """Получить текущий статус."""
    status = {
        "uptime_seconds": _metrics_store["uptime"],
        "last_scan": _metrics_store["last_scan"],
        "last_scan_duration": _metrics_store["last_scan_duration"],
        "scan_count": _metrics_store["scan_count"],
        "scan_errors": _metrics_store["scan_errors"],
        "version": _metrics_store["version"],
        "commit_hash": _metrics_store["commit_hash"],
        "is_update_pending": _metrics_store["is_update_pending"],
        "paper_cycles": _metrics_store["paper_cycles"],
        "paper_balance": float(_metrics_store["paper_balance"]) if _metrics_store["paper_balance"] else None,
    }
    
    # Если uptime есть, добавляем время аптайма
    if status["uptime_seconds"]:
        uptime_delta = time.time() - status["uptime_seconds"]
        status["uptime_human"] = _format_duration(uptime_delta)
    else:
        status["uptime_human"] = "unknown"
    
    # Добавляем время последнего скана
    if status["last_scan"]:
        last_scan_delta = time.time() - status["last_scan"]
        status["last_scan_delta_human"] = _format_duration(last_scan_delta) if last_scan_delta < 3600 else f"{last_scan_delta/60:.0f} мин назад"
    else:
        status["last_scan_delta_human"] = "never"
    
    return status


def _format_duration(seconds: float) -> str:
    """Форматировать длительность в читаемый вид."""
    if seconds < 60:
        return f"{seconds:.0f} сек"
    elif seconds < 3600:
        return f"{seconds/60:.1f} мин"
    elif seconds < 86400:
        return f"{seconds/3600:.1f} ч"
    else:
        return f"{seconds/86400:.1f} дн"


def set_metric(key: str, value):
    """Установить метрику (вызывается из бота)."""
    _metrics_store[key] = value


def clear_metric(key: str):
    """Очистить метрику."""
    if key in _metrics_store:
        del _metrics_store[key]


def update_scan_status(duration: float, errors: dict):
    """Обновить статус после скана."""
    _metrics_store["last_scan"] = time.time()
    _metrics_store["last_scan_duration"] = duration
    _metrics_store["scan_count"] += 1
    _metrics_store["scan_errors"] = errors


def start_metrics():
    """Начать сбор метрик (вызывается при старте бота)."""
    _metrics_store["uptime"] = time.time()
    logger.info("Health metrics started")


class MetricsCollector:
    """Коллектор метрик для Prometheus-формата."""
    
    def __init__(self):
        self._counters = {}
        self._gauges = {}
    
    def increment(self, name: str, value: float = 1):
        """Увеличить счётчик."""
        self._counters[name] = self._counters.get(name, 0) + value
    
    def set_gauge(self, name: str, value: float):
        """Установить Gauge-значение."""
        self._gauges[name] = value
    
    def to_prometheus(self) -> str:
        """Конвертировать в Prometheus-формат."""
        lines = []
        for name, value in self._counters.items():
            lines.append(f"{name} {value}")
        for name, value in self._gauges.items():
            lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"


# Глобальный коллектор
collector = MetricsCollector()


async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Обработчик /health — живой/мёртвый."""
    # Проверка пароля если задан
    password = request.query.get("token")
    expected_password = request.app["password"]
    if expected_password and password != expected_password:
        return aiohttp.web.Response(
            status=401,
            text="Unauthorized",
            content_type="text/plain"
        )
    
    # Проверяем, что бот запущен и не в режиме сна
    if _metrics_store["uptime"] is None:
        return aiohttp.web.Response(
            status=503,
            text="Bot not started",
            content_type="text/plain"
        )
    
    # Проверяем, что последний скан был не слишком давно
    if _metrics_store["last_scan"]:
        scan_age = time.time() - _metrics_store["last_scan"]
        if scan_age > 300:  # 5 минут
            return aiohttp.web.Response(
                status=503,
                text="Last scan too old",
                content_type="text/plain"
            )
    
    return aiohttp.web.Response(
        status=200,
        text="OK",
        content_type="text/plain"
    )


async def handle_status(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Обработчик /status — полная сводка."""
    password = request.query.get("token")
    expected_password = request.app["password"]
    if expected_password and password != expected_password:
        return aiohttp.web.Response(
            status=401,
            text="Unauthorized",
            content_type="text/plain"
        )
    
    status = get_status()
    return aiohttp.web.json_response(status)


async def handle_metrics(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Обработчик /metrics — Prometheus-метрики."""
    password = request.query.get("token")
    expected_password = request.app["password"]
    if expected_password and password != expected_password:
        return aiohttp.web.Response(
            status=401,
            text="Unauthorized",
            content_type="text/plain"
        )
    
    metrics_text = collector.to_prometheus()
    return aiohttp.web.Response(
        text=metrics_text,
        content_type="text/plain"
    )


async def handle_root(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Обработчик корня — список доступных эндпоинтов."""
    endpoints = [
        "GET /health — живой/мёртвый",
        "GET /status — полная сводка",
        "GET /metrics — Prometheus-метрики",
    ]
    text = "Health Check API for p2p-bot\n\n" + "\n".join(endpoints)
    return aiohttp.web.Response(
        text=text,
        content_type="text/plain"
    )


async def init_app():
    """Инициализация приложения и запуск сервера."""
    app = aiohttp.web.Application()
    app["password"] = os.environ.get("HEALTH_CHECK_PASSWORD")
    
    # Ручная регистрация маршрутов
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_get("/", handle_root)
    
    port = int(os.environ.get("HEALTH_CHECK_PORT", "8080"))
    
    logger.info(f"Health check server starting on port {port}")
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    return runner


def main():
    """Основная функция."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    
    runner = asyncio.run(init_app())
    logger.info("Health check server is running. Press Ctrl+C to stop.")
    
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down health check server")
        asyncio.run(runner.cleanup())


if __name__ == "__main__":
    main()
