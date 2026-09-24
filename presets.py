"""Пресеты фильтров: набор полей Config (assets, exchanges, include_pay, min_profit, amount),
применяются все сразу («И»). Пользовательские — JSON data/presets.json (папка data/ в git не попадает);
встроенные — «Только мои банки», «USDT без переводов», «Все площадки» — считаются от текущих cfg/env."""
import json
import os

from p2p import ALL_EXCHANGES, DEFAULT_ASSETS

HERE = os.path.dirname(os.path.abspath(__file__))
PRESETS_PATH = os.path.join(HERE, "data", "presets.json")
FIELDS = ("assets", "exchanges", "include_pay", "min_profit", "amount")  # поля пользовательского пресета


def _load(path=PRESETS_PATH):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(data, path=PRESETS_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_preset(name, cfg, path=PRESETS_PATH):
    """Сохранить текущие фильтры (assets/exchanges/include_pay/min_profit/amount) под именем name."""
    data = _load(path)
    data[name] = {k: getattr(cfg, k) for k in FIELDS}
    _save(data, path)


def list_custom(path=PRESETS_PATH):
    """{имя: поля} — пресеты, сохранённые пользователем."""
    return _load(path)


def delete_preset(name, path=PRESETS_PATH):
    data = _load(path)
    if name in data:
        del data[name]
        _save(data, path)


def builtin_presets(cfg):
    """Встроенные пресеты. «Только мои банки» показывается, только если в .env задан INCLUDE_PAY."""
    out = {}
    if cfg.include_pay:
        out["Только мои банки"] = {"include_pay": list(cfg.include_pay)}
    out["USDT без переводов"] = {"assets": ["USDT"], "same_venue_only": True}
    out["Все площадки"] = {"assets": DEFAULT_ASSETS.split(","), "exchanges": ALL_EXCHANGES.split(","),
                            "include_pay": [], "same_venue_only": False}
    return out


def get_preset(name, cfg, path=PRESETS_PATH):
    """Поля пресета по имени (встроенный или сохранённый пользователем); None — нет такого."""
    builtin = builtin_presets(cfg)
    if name in builtin:
        return builtin[name]
    return list_custom(path).get(name)
