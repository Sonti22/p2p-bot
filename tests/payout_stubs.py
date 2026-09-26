"""Заглушка бота для тестов гостей и выплат: сообщения в Telegram записываются в bot.out, сеть не нужна.

Защищённый файл (слово payout в имени): на этих заглушках стоят тесты выплат (tests/test_payouts*.py) — подмена
Stub в незащищённом файле сделала бы их пустыми. Правится только вручную, мерж — после проверки владельцем.
"""
import bot as B


class Stub(B.Bot):
    def __init__(self, cfg, guests=()):
        super().__init__(None, "x", "1", cfg)
        self.guests = set(guests)
        self.out, self.env = [], {}

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": len(self.out)}}

    async def _post_photo(self, png, caption, markup, thread=None, chat_id=None):
        return await self.call("sendPhoto", chat_id=chat_id or self.chat_id, caption=caption, reply_markup=markup,
                               message_thread_id=thread)


def sent(bot, method="sendMessage"):
    return [p for m, p in bot.out if m == method]


def msg(chat, text, **sender):
    return {"message": {"chat": {"id": chat}, "text": text, "from": {"first_name": "Вася", **sender}}}
