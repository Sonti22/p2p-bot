"""Торговые ключи Bybit/BingX (bybit_trade / bingx_trade) — вводит только владелец, на ПК, в этой консоли.

Ключ и секрет запрашиваются через getpass (на экран не выводятся), пишутся в data/keys.json папки бота зашифрованными
Windows DPAPI (accounts.save_key) и никогда не проходят через Telegram, логи или git. Ключ — «только торговля»:
без вывода и переводов, по возможности с привязкой к IP. После ввода можно сразу проверить права ключа у биржи.
Запуск: python scripts/trading_keys.py [set|check|delete] bybit|bingx [--bot-dir ПАПКА_БОТА]
"""
import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import accounts  # noqa: E402
from trading import keys as trade_keys  # noqa: E402
from trading import venues  # noqa: E402

DEFAULT_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YES = ("y", "yes", "д", "да")
HINT = {
    venues.BYBIT: "Bybit → API → Create New Key → System-generated: права только Contract (Orders, Positions) и, если "
                  "нужен спот, SPOT Trade; Wallet (Transfer, Withdraw), P2P, Earn и прочее — выключены; IP — этого ПК.",
    venues.BINGX: "BingX → API Management: права только Read и Perpetual Futures Trading; Universal Transfer, "
                  "Withdraw и Internal Transfer — выключены; IP — этого ПК.",
}


def _session():   # отдельно, чтобы тесты подменяли сеть
    import aiohttp
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))


async def _check(venue):
    async with _session() as s:
        return await trade_keys.check(s, venue)


def cmd_set(venue, ask=getpass.getpass, confirm=input):
    print(HINT[venue])
    key = ask(f"API key {venue} (не отображается): ").strip()
    secret = ask(f"API secret {venue} (не отображается): ").strip()
    if not key or not secret or any(c.isspace() for c in key + secret):
        print("Пустой ключ/секрет или пробелы внутри — ничего не сохранено.")
        return 1
    accounts.save_key(trade_keys.KEY_NAMES[venue], key, secret)
    print(f"Сохранено: {trade_keys.KEY_NAMES[venue]} {accounts.mask(key)} (data/keys.json, DPAPI).")
    if confirm("Проверить права ключа у биржи сейчас? [y/N] ").strip().lower() in YES:
        return cmd_check(venue)
    return 0


def cmd_check(venue, run=None):
    res = (run or (lambda v: asyncio.run(_check(v))))(venue)
    print(("✅ ключ годится для торговли" if res.ok else f"⛔ торговля по ключу запрещена ({res.state})")
          + (f": {res.detail}" if res.detail else ""))
    return 0 if res.ok else 2


def cmd_delete(venue):
    ok = accounts.delete_key(trade_keys.KEY_NAMES[venue])
    print("Ключ удалён." if ok else "Ключ не был сохранён.")
    return 0


def main(argv=None, ask=getpass.getpass, confirm=input, run=None):
    p = argparse.ArgumentParser(description="Торговые ключи Bybit/BingX (только владелец, на ПК)")
    p.add_argument("cmd", choices=("set", "check", "delete"))
    p.add_argument("venue", choices=venues.VENUES)
    p.add_argument("--bot-dir", default=DEFAULT_BOT_DIR)
    a = p.parse_args(argv)
    accounts.KEYS_PATH = os.path.join(a.bot_dir, "data", "keys.json")   # ключи — в папку бота, а не в текущую
    if a.cmd == "set":
        if ask is getpass.getpass and not sys.stdin.isatty():
            print("Нужна интерактивная консоль владельца (ввод ключа через getpass).")
            return 1
        return cmd_set(a.venue, ask, confirm)
    if a.cmd == "check":
        return cmd_check(a.venue, run)
    return cmd_delete(a.venue)


if __name__ == "__main__":
    sys.exit(main())
