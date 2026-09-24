"""Пресеты фильтров: набор полей Config (assets, exchanges, include_pay, min_profit, amount, same_venue_only),
применяются все сразу («И»). Пользовательские — JSON data/presets.json (папка data/ в git не попадает);
встроенные — «Только мои банки», «USDT без переводов», «Все площадки» — считаются от текущих cfg/env.
В кнопках пресет адресуется коротким id (preset_id), а не именем: callback_data у Telegram — до 64 байт."""
import hashlib
import os

import jsonstore
from p2p import ALL_EXCHANGES, DEFAULT_ASSETS

HERE = os.path.dirname(os.path.abspath(__file__))
PRESETS_PATH = os.path.join(HERE, "data", "presets.json")
# поля пользовательского пресета; новое поле — добавить и в env_map у Bot.apply_preset
FIELDS = ("assets", "exchanges", "include_pay", "min_profit", "amount", "same_venue_only")


def _load(path=PRESETS_PATH):
    return jsonstore.read_dict(path)


def _save(data, path=PRESETS_PATH):
    jsonstore.write_dict(path, data)


def save_preset(name, cfg, path=PRESETS_PATH):
    """Сохранить текущие фильтры (assets/exchanges/include_pay/min_profit/amount/same_venue_only) под именем name."""
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


def preset_id(name):
    """Короткий стабильный id для callback_data: имя кириллицей (2 байта на букву) в 64 байта не влезает."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def name_by_id(pid, cfg, path=PRESETS_PATH):
    """Имя пресета (встроенного или сохранённого) по id из кнопки; None — такого уже нет."""
    for name in [*builtin_presets(cfg), *_load(path)]:
        if preset_id(name) == pid:
            return name
    return None
