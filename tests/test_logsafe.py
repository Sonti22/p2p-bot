"""logsafe.redact() и RedactingFormatter: токен бота Telegram и секретные параметры запросов не должны попадать
в logs/bot.log, консоль и /logs, даже когда исключение (aiohttp) или чужой код логируют их сырым текстом.
Без сети, без реальных токенов литералом (TOKEN собран конкатенацией, как того требует guard SECRETS)."""
import asyncio
import logging
import re

import aiohttp
import multidict
import pytest
import yarl

import bot as B
import logsafe
import p2p
from helpers import arun
from test_bot import Stub

TOKEN = "123456789" + ":" + "A" * 35


def _content_type_error(status=502, message="x"):
    url = yarl.URL(f"https://api.telegram.org/bot{TOKEN}/getUpdates")
    info = aiohttp.client_reqrep.RequestInfo(url, "POST", multidict.CIMultiDict(), url)
    return aiohttp.ContentTypeError(info, (), status=status, message=message)


# --- redact() ----------------------------------------------------------------------------------------------------

def test_redact_hides_token_in_url_and_str_content_type_error():
    exc = _content_type_error()
    assert TOKEN in str(exc)   # проверка не проходит вхолостую
    text = logsafe.redact(str(exc))
    assert TOKEN not in text and "<токен скрыт>" in text


def test_redact_hides_bare_token_outside_url():
    text = logsafe.redact(f"токен бота: {TOKEN} — не публикуй")
    assert TOKEN not in text and "<токен скрыт>" in text


def test_redact_hides_secret_query_params_on_allowed_domains():
    urls = ("https://api.telegram.org/x", "https://api.mexc.com/x", "https://api.bybit.com/x",
            "https://api.htx.com/x")
    for base in urls:
        for name in ("signature", "Signature", "AccessKeyId", "api_key", "API_KEY", "apikey", "access_token",
                     "token", "secret"):
            text = logsafe.redact(f"GET {base}?{name}=abcDEF123&foo=1")
            assert "abcDEF123" not in text
            assert f"{name}=***" in text
            assert "foo=1" in text   # обычный параметр не тронут


def test_redact_hides_authorization_header_and_bearer():
    assert logsafe.redact("Authorization: Bearer abc.def-123") == "Authorization: ***"
    assert logsafe.redact("authorization=Basic dXNlcjpwYXNz") == "authorization=***"
    assert logsafe.redact("Bearer abc.def-123") == "Bearer ***"


def test_redact_leaves_ordinary_text_unchanged():
    samples = ["обычный текст лога", "12:34:56", "97.5%", "design=1 — не секрет", "id 12345", "2026-09-30",
               "жёлтый % риска: 3"]
    for s in samples:
        assert logsafe.redact(s) == s


def test_redact_is_idempotent():
    text = f"bot{TOKEN} signature=xyz&next=1 Bearer abc123 Authorization: Bearer q.w-e_r"
    once = logsafe.redact(text)
    assert logsafe.redact(once) == once


def test_redact_does_not_crash_on_non_strings():
    assert logsafe.redact(None) == "None"
    assert logsafe.redact(123) == "123"
    text = logsafe.redact(_content_type_error())
    assert TOKEN not in text and "<токен скрыт>" in text


# --- p2p.setup_logging (RedactingFormatter) -----------------------------------------------------------------------

def test_setup_logging_redacts_file_and_console(tmp_path, capsys):
    log_path = str(tmp_path / "bot.log")
    p2p.setup_logging(log_path)
    exc = _content_type_error()
    logging.getLogger("logsafe_test_file").warning("getUpdates error: %s", str(exc))
    content = open(log_path, encoding="utf-8").read()
    assert TOKEN not in content and "<токен скрыт>" in content
    first_line = content.splitlines()[0]
    assert re.match(r"^\d\d\.\d\d \d\d:\d\d:\d\d WARNING logsafe_test_file: ", first_line)
    out = capsys.readouterr().out
    assert TOKEN not in out and "<токен скрыт>" in out


def test_setup_logging_redacts_traceback_via_exception(tmp_path):
    log_path = str(tmp_path / "bot.log")
    p2p.setup_logging(log_path)
    logger = logging.getLogger("logsafe_test_exc")
    try:
        raise RuntimeError(f"boom {TOKEN}")
    except RuntimeError:
        logger.exception("failure")
    content = open(log_path, encoding="utf-8").read()
    assert TOKEN not in content and "<токен скрыт>" in content


def test_command_loop_update_error_redacted_by_formatter_even_with_raw_e(tmp_path, monkeypatch):
    """bot.py update error логирует сырое исключение — маскирует форматтер, а не api_error_text."""
    log_path = str(tmp_path / "bot.log")
    p2p.setup_logging(log_path)
    bot = Stub(p2p.Config())

    async def bad_on_update(u):
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(bot, "on_update", bad_on_update)
    calls = {"n": 0}

    async def fake_call(method, **params):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError
        return {"ok": True, "result": [{"update_id": 1}]}

    monkeypatch.setattr(bot, "call", fake_call)
    with pytest.raises(asyncio.CancelledError):
        arun(bot.command_loop())
    content = open(log_path, encoding="utf-8").read()
    assert TOKEN not in content and "<токен скрыт>" in content


# --- logs_view -----------------------------------------------------------------------------------------------

def test_logs_view_redacts_already_written_token(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(f"30.09 10:00:00 WARNING x: getUpdates error: HTTP 502, url='.../bot{TOKEN}/x' <script>bad</script>\n",
                   encoding="utf-8")
    text = B.logs_view(str(log))
    assert TOKEN not in text
    assert "&lt;токен скрыт&gt;" in text   # redact() ставит "<токен скрыт>" до html.escape — экранируется вместе с текстом
    assert "&lt;script&gt;" in text and "<script>bad" not in text


# --- bot.py:3620 (getUpdates) и 4383 (setup) — accounts.api_error_text вместо сырого e -----------------------------

def test_get_updates_error_uses_api_error_text_no_token(monkeypatch, caplog):
    bot = Stub(p2p.Config())
    exc = _content_type_error()

    async def raiser(method, **params):
        raise exc

    monkeypatch.setattr(bot, "call", raiser)

    async def no_sleep(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(B.asyncio, "sleep", no_sleep)
    caplog.set_level(logging.WARNING)
    with pytest.raises(asyncio.CancelledError):
        arun(bot.command_loop())
    messages = [r.getMessage() for r in caplog.records]
    assert not any(TOKEN in m for m in messages)
    assert any("HTTP 502" in m for m in messages)


def test_setup_error_uses_api_error_text_no_token(monkeypatch, caplog):
    bot = Stub(p2p.Config())
    exc = _content_type_error()

    async def raiser(method, **params):
        raise exc

    monkeypatch.setattr(bot, "call", raiser)
    caplog.set_level(logging.WARNING)
    arun(bot.setup())
    messages = [r.getMessage() for r in caplog.records]
    assert not any(TOKEN in m for m in messages)
    assert any("HTTP 502" in m for m in messages)
