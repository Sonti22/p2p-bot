"""Маскировка токена Telegram-бота и секретных параметров запросов (signature, api_key, token, Authorization/
Bearer...) в логах: RedactingFormatter применяет redact() к уже отформатированной строке (сообщение и трейсбек),
logs_view — к уже записанным на диск строкам. Только logging и re: без сети, без импорта bot/p2p/accounts."""
import logging
import re

_TOKEN_IN_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_BARE_TOKEN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}")
_SECRET_PARAM = re.compile(r"(?i)\b(signature|sign|accesskeyid|api_key|apikey|access_token|token|secret)=[^&\s'\"]+")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:(?:bearer|basic)\s+)?[^\s'\",;]+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[\w.~+/=-]+")


def redact(text):
    """Скрыть токен бота (в пути Bot API и голый), секретные query-параметры и заголовок Authorization/Bearer.
    text приводится к str() (не падает на None, числах, исключениях). Идемпотентна: redact(redact(x)) == redact(x)."""
    text = str(text)
    text = _TOKEN_IN_PATH.sub("/bot<токен скрыт>", text)
    text = _BARE_TOKEN.sub("<токен скрыт>", text)
    text = _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=***", text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}***", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}***", text)
    return text


class RedactingFormatter(logging.Formatter):
    """logging.Formatter, маскирующий итоговую строку (сообщение и трейсбек, если есть) через redact()."""

    def format(self, record):
        return redact(super().format(record))
