import asyncio
import atexit

import p2p


def make_ad(ex="Bybit", side="buy", price=85.0, asset="USDT", pays=("T-Bank",), orders=200, rate=100.0,
            net="", url="", min_amt=1000, max_amt=500000, avail=10000, terms=""):
    return p2p.Ad(ex, side, price, min_amt, max_amt, avail, list(pays), "nick", orders, rate, url, asset, net, terms)


# --- один цикл событий на весь прогон тестов ---------------------------------------------------------------------
# asyncio.run() на каждый вызов создаёт и закрывает новый цикл: на Windows (Proactor) это новая пара сокетов и
# self-pipe каждый раз — за сотни вызовов кончаются сокеты (WinError 10055) и тесты зависают. arun() делает то же,
# что asyncio.run (Python 3.10: new_event_loop + run_until_complete), но на одном цикле: после каждого вызова, как
# asyncio.run, отменяет задачи, оставшиеся после корутины, и закрывает асинхронные генераторы — тесты не видят чужих
# фоновых задач. Цикл закрывается один раз, при выходе из процесса тестов.
_loop = None


def _close_loop():
    global _loop
    if _loop is not None and not _loop.is_closed():
        _cancel_leftovers(_loop)
        _loop.run_until_complete(_loop.shutdown_asyncgens())
        _loop.run_until_complete(_loop.shutdown_default_executor())
        _loop.close()
    _loop = None


def _cancel_leftovers(loop):
    tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))


def arun(coro):
    """asyncio.run(coro) на общем цикле событий тестов (см. выше): вернуть результат или поднять исключение корутины."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        return _loop.run_until_complete(coro)
    finally:
        _cancel_leftovers(_loop)
        _loop.run_until_complete(_loop.shutdown_asyncgens())
        asyncio.set_event_loop(None)


atexit.register(_close_loop)
