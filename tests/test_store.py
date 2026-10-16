import json
import os

import pytest

import fdm.store as store
from fdm.store import write_json


def test_write_json_replaces_the_file_whole(tmp_path):
    path = tmp_path / "bids" / "shop-1.json"
    write_json(path, [{"bid_id": 1}])
    write_json(path, [{"bid_id": 1}, {"bid_id": 2}])
    assert path.read_text(encoding="utf-8").count("bid_id") == 2
    assert [p.name for p in path.parent.iterdir()] == ["shop-1.json"]  # no temporary file left behind


def test_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "shop-1.json"
    write_json(path, ["old"])

    def crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        write_json(path, ["new"])
    assert json.loads(path.read_text(encoding="utf-8")) == ["old"]
    assert [p.name for p in tmp_path.iterdir()] == ["shop-1.json"]


def test_a_file_held_open_by_a_reader_is_replaced_once_it_is_free(tmp_path, monkeypatch):
    path, attempts = tmp_path / "shop-1.json", []
    real_replace = os.replace

    def busy_then_free(src, dst):  # what Windows does while another thread is reading the file
        attempts.append(dst)
        if len(attempts) < 3:
            raise PermissionError(13, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", busy_then_free)
    monkeypatch.setattr(store.time, "sleep", lambda seconds: None)
    write_json(path, ["new"])
    assert len(attempts) == 3 and "new" in path.read_text(encoding="utf-8")


def test_a_file_that_stays_locked_fails_the_write_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "shop-1.json"
    write_json(path, ["old"])

    def always_busy(src, dst):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(store.os, "replace", always_busy)
    monkeypatch.setattr(store.time, "sleep", lambda seconds: None)
    with pytest.raises(PermissionError):
        write_json(path, ["new"])
    assert json.loads(path.read_text(encoding="utf-8")) == ["old"]
    assert [p.name for p in tmp_path.iterdir()] == ["shop-1.json"]
