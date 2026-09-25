"""Избранные маршруты владельца: data/favorites.json — {"Bybit|USDT|MEXC|USDT": добавлен (ts)}. Маршрут —
площадка/монета покупки → площадка/монета продажи (как Bot._deal_key). По избранным сигнал приходит от
FAV_MIN_PROFIT (по умолчанию 1%), даже ниже общего порога и вне топа."""
import os
import time

import jsonstore

HERE = os.path.dirname(os.path.abspath(__file__))
FAV_PATH = os.path.join(HERE, "data", "favorites.json")


def key_str(key):
    return "|".join(key)


def keys():
    return set(jsonstore.read_dict(FAV_PATH))


def is_fav(key):
    return key_str(key) in keys()


def toggle(key):
    """Добавить маршрут в избранное или убрать; True — теперь в избранном."""
    data = jsonstore.read_dict(FAV_PATH)
    k = key_str(key)
    if k in data:
        del data[k]
    else:
        data[k] = time.time()
    jsonstore.write_dict(FAV_PATH, data)
    return k in data


def remove(k):
    data = jsonstore.read_dict(FAV_PATH)
    found = data.pop(k, None) is not None
    if found:
        jsonstore.write_dict(FAV_PATH, data)
    return found


def fav_min_profit():
    try:
        return float(os.getenv("FAV_MIN_PROFIT", 1.0))
    except ValueError:
        return 1.0


def label(k):
    """«Bybit USDT → MEXC USDT» для списка /fav."""
    b_ex, b_asset, s_ex, s_asset = (k.split("|") + ["", "", "", ""])[:4]
    return f"{b_ex} {b_asset} → {s_ex} {s_asset}"
