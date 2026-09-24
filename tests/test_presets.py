import json

import p2p
import presets


def cfg(**kw):
    c = p2p.Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_save_preset_captures_five_fields(tmp_path):
    path = str(tmp_path / "presets.json")
    c = cfg(assets=["USDT"], exchanges=["bybit", "mexc"], include_pay=["sberbank"], min_profit=3.0, amount=70000)
    presets.save_preset("Мой набор", c, path=path)
    saved = json.loads((tmp_path / "presets.json").read_text(encoding="utf-8"))
    assert saved["Мой набор"] == {"assets": ["USDT"], "exchanges": ["bybit", "mexc"],
                                  "include_pay": ["sberbank"], "min_profit": 3.0, "amount": 70000}


def test_list_custom_empty_when_no_file(tmp_path):
    assert presets.list_custom(path=str(tmp_path / "none.json")) == {}


def test_get_preset_returns_saved_fields(tmp_path):
    path = str(tmp_path / "presets.json")
    presets.save_preset("Быстрый", cfg(assets=["USDT"]), path=path)
    assert presets.get_preset("Быстрый", cfg(), path=path)["assets"] == ["USDT"]


def test_get_preset_unknown_name_is_none(tmp_path):
    assert presets.get_preset("нет такого", cfg(), path=str(tmp_path / "presets.json")) is None


def test_delete_preset_removes_entry(tmp_path):
    path = str(tmp_path / "presets.json")
    presets.save_preset("Временный", cfg(), path=path)
    assert "Временный" in presets.list_custom(path=path)
    presets.delete_preset("Временный", path=path)
    assert "Временный" not in presets.list_custom(path=path)


def test_delete_preset_missing_name_is_noop(tmp_path):
    path = str(tmp_path / "presets.json")
    presets.delete_preset("нет такого", path=path)   # не должно упасть, файла ещё нет
    assert presets.list_custom(path=path) == {}


def test_builtin_presets_includes_bank_filter_only_when_env_set():
    with_banks = presets.builtin_presets(cfg(include_pay=["sberbank"]))
    without_banks = presets.builtin_presets(cfg(include_pay=[]))
    assert "Только мои банки" in with_banks and with_banks["Только мои банки"]["include_pay"] == ["sberbank"]
    assert "Только мои банки" not in without_banks


def test_builtin_preset_usdt_no_transfer_sets_same_venue_only():
    fields = presets.builtin_presets(cfg())["USDT без переводов"]
    assert fields["assets"] == ["USDT"] and fields["same_venue_only"] is True


def test_builtin_preset_all_venues_resets_filters():
    fields = presets.builtin_presets(cfg())["Все площадки"]
    assert set(fields["exchanges"]) == set(p2p.ALL_EXCHANGES.split(","))
    assert set(fields["assets"]) == set(p2p.DEFAULT_ASSETS.split(","))
    assert fields["include_pay"] == [] and fields["same_venue_only"] is False


def test_get_preset_prefers_builtin_over_custom_name_clash(tmp_path):
    path = str(tmp_path / "presets.json")
    presets.save_preset("Все площадки", cfg(assets=["BTC"]), path=path)
    fields = presets.get_preset("Все площадки", cfg(), path=path)
    assert fields["assets"] != ["BTC"]   # встроенный пресет главнее одноимённого пользовательского
