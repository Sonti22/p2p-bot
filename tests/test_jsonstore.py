"""Атомарная запись JSON (keys.json/presets.json/topics.json) и защита чтения от битого файла."""
import json
import logging
import os

import jsonstore


def test_write_dict_creates_folder_and_readable_file(tmp_path):
    path = str(tmp_path / "sub" / "data.json")
    jsonstore.write_dict(path, {"a": 1})
    assert json.loads(open(path, encoding="utf-8").read()) == {"a": 1}


def test_write_dict_leaves_no_temp_file_behind(tmp_path):
    path = str(tmp_path / "data.json")
    jsonstore.write_dict(path, {"a": 1})
    assert os.listdir(tmp_path) == ["data.json"]   # только целевой файл, временный убран os.replace


def test_write_dict_replaces_existing_file_without_gap(tmp_path):
    path = tmp_path / "data.json"
    path.write_text('{"old": true}', encoding="utf-8")
    jsonstore.write_dict(str(path), {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}


def test_write_dict_cleans_up_temp_file_on_failure(tmp_path, monkeypatch):
    """Объект, который json не умеет сериализовать, не должен оставлять .tmp-* файл в папке."""
    path = str(tmp_path / "data.json")
    try:
        jsonstore.write_dict(path, {"bad": object()})
    except TypeError:
        pass
    assert os.listdir(tmp_path) == []


def test_read_dict_missing_file_is_empty(tmp_path):
    assert jsonstore.read_dict(str(tmp_path / "none.json")) == {}


def test_read_dict_round_trip(tmp_path):
    path = str(tmp_path / "data.json")
    jsonstore.write_dict(path, {"x": [1, 2]})
    assert jsonstore.read_dict(path) == {"x": [1, 2]}


def test_read_dict_broken_json_is_empty_with_warning(tmp_path, caplog):
    path = tmp_path / "data.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jsonstore"):
        assert jsonstore.read_dict(str(path)) == {}
    assert "битый JSON" in caplog.text


def test_read_dict_list_instead_of_dict_is_empty_with_warning(tmp_path, caplog):
    path = tmp_path / "data.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jsonstore"):
        assert jsonstore.read_dict(str(path)) == {}
    assert "вместо словаря" in caplog.text
