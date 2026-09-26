"""Белый список адресов выплат Cryptomus — data/payout_whitelist.json в папке бота. Правит только владелец, на ПК.

Бот файл только читает и платит только на адреса из него (/payout). Адрес при добавлении вводится дважды, запись
проверяется теми же правилами, что и в боте (payouts.validate_entry). Ключей скрипт не касается.
Запуск: python scripts/payout_whitelist.py [list|add|remove] [--bot-dir ПАПКА_БОТА]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jsonstore  # noqa: E402
import payouts  # noqa: E402

DEFAULT_BOT_DIR = r"C:\Users\User\Desktop\p2p-bot"
YES = ("y", "yes", "д", "да")


def whitelist_path(bot_dir):
    return os.path.join(bot_dir, "data", "payout_whitelist.json")


def read_entries(path):
    """Записи как есть (и с ошибками — их не выбрасываем молча, бот их просто пропустит)."""
    raw = jsonstore.read_dict(path).get("entries")
    return raw if isinstance(raw, list) else []


def show(e):
    memo = f", memo {e.get('memo')}" if e.get("memo") else ""
    return f"[{e.get('id')}] {e.get('name')}: {e.get('currency')} · {e.get('network')} · {e.get('address')}{memo}"


def next_id(entries):
    nums = [int(str(e.get("id"))[1:]) for e in entries
            if isinstance(e, dict) and str(e.get("id", "")).startswith("w") and str(e.get("id"))[1:].isdigit()]
    return f"w{max(nums, default=0) + 1}"


def cmd_list(path):
    entries = read_entries(path)
    if not entries:
        print("Белый список пуст.")
        return 0
    for e in entries:
        _, why = payouts.validate_entry(e)
        print(show(e) if isinstance(e, dict) else repr(e), f"  ⚠️ бот пропустит: {why}" if why else "")
    return 0


def cmd_add(path, ask=input):
    entries = read_entries(path)
    cur = ask(f"Монета ({', '.join(payouts.COINS)}): ").strip().upper()
    if cur not in payouts.COINS:
        print("Такой монеты нет в списке — ничего не записано.")
        return 1
    net = ask(f"Сеть ({', '.join(payouts.COIN_NETWORKS[cur])}): ").strip().lower()
    if net not in payouts.COIN_NETWORKS[cur]:
        print(f"Сеть не из списка для {cur} — ничего не записано.")
        return 1
    name = ask("Имя получателя (на кнопке в боте, до 40 символов): ").strip()
    address = ask("Адрес: ").strip()
    why = payouts.address_error(net, address)
    if why:
        print(f"Адрес не подходит: {why} — ничего не записано.")
        return 1
    if payouts.NETWORKS.get(net) == "evm" and not payouts.evm_checksummed(address):
        print("⚠️ Адрес без контрольной суммы EIP-55 (все буквы одного регистра): опечатку в нём не поймать. Лучше "
              "скопируй адрес со смешанным регистром букв, как его показывает кошелёк или биржа.")
        if ask("Всё равно добавить адрес без контрольной суммы? [y/N]: ").strip().lower() not in YES:
            print("Отменено — ничего не записано.")
            return 1
    if ask("Адрес ещё раз (вставь заново): ").strip() != address:
        print("Адреса не совпали — ничего не записано.")
        return 1
    memo = ask("Memo (нужен для TON-адресов бирж; Enter — без memo): ").strip() if net == "ton" else ""
    entry = {"id": next_id(entries), "name": name, "currency": cur, "network": net, "address": address}
    if memo:
        entry["memo"] = memo
    _, why = payouts.validate_entry(entry)
    if why:
        print(f"Запись не подходит: {why} — ничего не записано.")
        return 1
    same = [e for e in entries if isinstance(e, dict) and e.get("address") == address and e.get("network") == net
            and str(e.get("currency", "")).upper() == cur and (e.get("memo") or "") == memo]
    if same:
        print(f"Такой адрес уже есть: {show(same[0])} — ничего не записано.")
        return 1
    print("Новая запись:\n  " + show(entry))
    print("Проверь адрес и сеть: выплату на неверный адрес или в чужую сеть не вернуть.")
    if ask("Записать? [y/N]: ").strip().lower() not in YES:
        print("Отменено — ничего не записано.")
        return 1
    entries.append(entry)
    jsonstore.write_dict(path, {"entries": entries})
    print(f"Записано в {path}. Бот увидит запись при следующем /payout.")
    return 0


def cmd_remove(path, ask=input):
    entries = read_entries(path)
    if not entries:
        print("Белый список пуст.")
        return 1
    cmd_list(path)
    eid = ask("id записи для удаления: ").strip()
    match = [e for e in entries if isinstance(e, dict) and str(e.get("id")) == eid]
    if not match:
        print("Нет записи с таким id — ничего не изменено.")
        return 1
    print("Удалить:\n  " + show(match[0]))
    if ask("Удалить? [y/N]: ").strip().lower() not in YES:
        print("Отменено — ничего не изменено.")
        return 1
    jsonstore.write_dict(path, {"entries": [e for e in entries if not any(e is m for m in match)]})
    print(f"Удалено из {path}.")
    return 0


def main(argv=None, ask=input):
    p = argparse.ArgumentParser(description="Белый список адресов выплат Cryptomus (только на ПК владельца)")
    p.add_argument("command", nargs="?", default="list", choices=("list", "add", "remove"))
    p.add_argument("--bot-dir", default=DEFAULT_BOT_DIR, help="папка бота (по умолчанию %(default)s)")
    args = p.parse_args(argv)
    if not os.path.isdir(args.bot_dir):
        print(f"Нет папки бота: {args.bot_dir}")
        return 1
    path = whitelist_path(args.bot_dir)
    print(f"Файл: {path}")
    if args.command == "add":
        return cmd_add(path, ask)
    if args.command == "remove":
        return cmd_remove(path, ask)
    return cmd_list(path)


if __name__ == "__main__":
    sys.exit(main())
