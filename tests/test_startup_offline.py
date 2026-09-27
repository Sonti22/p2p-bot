"""Старт без сети (ПК проснулся, VPN/DNS ещё не поднялись): бот не падает, топики — из сохранённого файла.
26.09 в 10:50 бот трижды завершился с кодом 1 на getMe, и launcher счёл это падением при запуске."""

import aiohttp

import bot as B
import p2p
from test_bot import Stub
from helpers import arun


class Offline(Stub):
    async def call(self, method, **p):
        self.out.append((method, p))
        raise aiohttp.ClientConnectionError("Cannot connect to host api.telegram.org:443 [getaddrinfo failed]")


def test_setup_and_topics_survive_no_network():
    B.save_topics({"signals": 11, "journal": 12, "settings": 13, "dev": 14})
    bot = Offline(p2p.Config())
    arun(bot.setup())
    arun(bot.setup_topics())                     # раньше — исключение и exit 1
    assert bot.topics == {"signals": 11, "journal": 12, "settings": 13, "dev": 14}


def test_create_topic_network_error_is_not_fatal():
    class TopicsOn(Stub):
        async def call(self, method, **p):
            self.out.append((method, p))
            if method == "getMe":
                return {"ok": True, "result": {"username": "p2pbot", "has_topics_enabled": True}}
            raise aiohttp.ClientConnectionError("reset")
    bot = TopicsOn(p2p.Config())
    arun(bot.setup_topics())
    assert bot.username == "p2pbot" and bot.topics == {}
