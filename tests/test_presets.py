import json

import p2p
import presets


def cfg(**kw):
    c = p2p.Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_save_preset_captures_all_filter_fields(tmp_path):
    path = str(tmp_path / "presets.json")
    c = cfg(assets=["USDT"], exchanges=["bybit", "mexc"], include_pay=["sberbank"], min_profit=3.0, amount=70000,
            same_venue_only=True)
    presets.save_preset("Мой набор", c, path=path)
    saved = json.loads((tmp_path / "presets.json").read_text(encoding="utf-8"))
    assert saved["Мой набор"] == {"assets": ["USDT"], "exchanges": ["bybit", "mexc"],
                                  "include_pay": ["sberbank"], "min_profit": 3.0, "amount": 70000,
                                  "same_venue_only": True}


def test_fields_cover_everything_builtin_presets_set():
    """Свой пресет должен уметь вернуть всё, что меняют встроенные (иначе «Мой» после «Все площадки» неполный)."""
    builtin_keys = {k for fields in presets.builtin_presets(cfg(include_pay=["sberbank"])).values() for k in fields}
    assert builtin_keys <= set(presets.FIELDS)


def test_get_preset_legacy_entry_without_same_venue_only(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text('{"Старый": {"assets": ["USDT"], "min_profit": 2.0}}', encoding="utf-8")
    assert presets.get_preset("Старый", cfg(), path=str(path)) == {"assets": ["USDT"], "min_profit": 2.0}


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


def test_preset_id_is_short_and_stable():
    pid = presets.preset_id("а" * 40)
    assert len(("preset_apply:" + pid).encode("utf-8")) <= 64
    assert pid == presets.preset_id("а" * 40)
    assert pid != presets.preset_id("а" * 39)


def test_name_by_id_finds_builtin_and_custom(tmp_path):
    path = str(tmp_path / "presets.json")
    presets.save_preset("Мой", cfg(), path=path)
    assert presets.name_by_id(presets.preset_id("Мой"), cfg(), path=path) == "Мой"
    assert presets.name_by_id(presets.preset_id("Все площадки"), cfg(), path=path) == "Все площадки"
    assert presets.name_by_id(presets.preset_id("нет такого"), cfg(), path=path) is None
    assert presets.name_by_id("Мой", cfg(), path=path) is None   # имя — не id
