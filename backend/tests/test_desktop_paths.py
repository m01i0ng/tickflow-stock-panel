"""桌面版数据路径: frozen platformdirs、DATA_DIR 覆盖、旧目录迁移。"""
from __future__ import annotations

import sys
from pathlib import Path

from app.config import _IS_FROZEN, _PROJECT_ROOT, Settings, _dotenv_path, _user_data_root
from app.tickflow.repository import DataStore, _legacy_desktop_data_dirs


def test_dev_data_dir_is_project_root_data():
    assert _IS_FROZEN is False
    assert _user_data_root() == _PROJECT_ROOT / "data"
    assert Path(_dotenv_path()) == _PROJECT_ROOT / ".env"


def test_frozen_win32_uses_local_app_data(monkeypatch, tmp_path):
    import app.config as config

    expected = tmp_path / "AppData" / "Local" / "TickFlowStockPanel"
    seen: dict[str, object] = {}

    def fake_user_data_dir(appname, appauthor=False, **_k):
        seen["appname"] = appname
        seen["appauthor"] = appauthor
        return str(expected)

    monkeypatch.setattr(config, "_IS_FROZEN", True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("platformdirs.user_data_dir", fake_user_data_dir)
    assert config._user_data_root() == expected
    assert seen == {"appname": "TickFlowStockPanel", "appauthor": False}
    assert Path(config._dotenv_path()) == expected / ".env"


def test_frozen_darwin_uses_application_support(monkeypatch, tmp_path):
    import app.config as config

    expected = tmp_path / "Library" / "Application Support" / "TickFlowStockPanel"
    seen: dict[str, object] = {}

    def fake_user_data_dir(appname, appauthor=False, **_k):
        seen["appname"] = appname
        seen["appauthor"] = appauthor
        return str(expected)

    monkeypatch.setattr(config, "_IS_FROZEN", True)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("platformdirs.user_data_dir", fake_user_data_dir)
    assert config._user_data_root() == expected
    assert seen == {"appname": "TickFlowStockPanel", "appauthor": False}
    assert Path(config._dotenv_path()) == expected / ".env"


def test_data_dir_absolute_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom-data"
    monkeypatch.setenv("DATA_DIR", str(custom))
    settings = Settings()
    assert settings.data_dir == custom.resolve()


def test_frozen_relative_data_dir_not_project_root(monkeypatch, tmp_path):
    import app.config as config

    expected = tmp_path / "TickFlowStockPanel"
    monkeypatch.setattr(config, "_IS_FROZEN", True)
    monkeypatch.setattr(
        "platformdirs.user_data_dir",
        lambda *_a, **_k: str(expected),
    )
    monkeypatch.setenv("DATA_DIR", "./data")
    settings = config.Settings()
    assert settings.data_dir == expected
    assert settings.data_dir != (_PROJECT_ROOT / "data").resolve()


def test_legacy_dirs_include_exe_data_and_sibling(tmp_path):
    exe_dir = tmp_path / "TickFlowStockPanel"
    dirs = _legacy_desktop_data_dirs(exe_dir)
    assert dirs == [
        exe_dir / "data",
        exe_dir.parent / "TickFlowStockPanel_Data",
    ]


def _frozen_exe(monkeypatch, exe_dir: Path) -> None:
    exe_dir.mkdir(parents=True, exist_ok=True)
    exe = exe_dir / "TickFlowStockPanel.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))


def test_migrates_exe_dir_data_when_new_empty(monkeypatch, tmp_path):
    exe_dir = tmp_path / "install"
    _frozen_exe(monkeypatch, exe_dir)
    old = exe_dir / "data"
    old.mkdir()
    (old / "kline.parquet").write_bytes(b"old")
    new = tmp_path / "TickFlowStockPanel"

    DataStore(new)

    assert (new / "kline.parquet").read_bytes() == b"old"
    assert not old.exists()


def test_migrates_sibling_tickflow_data_dir(monkeypatch, tmp_path):
    exe_dir = tmp_path / "Programs" / "TickFlowStockPanel"
    _frozen_exe(monkeypatch, exe_dir)
    old = exe_dir.parent / "TickFlowStockPanel_Data"
    old.mkdir()
    (old / "notes.jsonl").write_text("x", encoding="utf-8")
    new = tmp_path / "Local" / "TickFlowStockPanel"

    DataStore(new)

    assert (new / "notes.jsonl").read_text(encoding="utf-8") == "x"
    assert not old.exists()


def test_skip_migration_when_new_has_parquet(monkeypatch, tmp_path):
    exe_dir = tmp_path / "install"
    _frozen_exe(monkeypatch, exe_dir)
    old = exe_dir / "data"
    old.mkdir()
    (old / "old.parquet").write_bytes(b"old")
    new = tmp_path / "TickFlowStockPanel"
    new.mkdir()
    (new / "keep.parquet").write_bytes(b"keep")

    DataStore(new)

    assert (old / "old.parquet").exists()
    assert (new / "keep.parquet").read_bytes() == b"keep"
    assert not (new / "old.parquet").exists()


def test_migration_failure_does_not_block_startup(monkeypatch, tmp_path):
    exe_dir = tmp_path / "install"
    _frozen_exe(monkeypatch, exe_dir)
    old = exe_dir / "data"
    old.mkdir()
    (old / "kline.parquet").write_bytes(b"old")
    new = tmp_path / "TickFlowStockPanel"

    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr("shutil.move", _boom)
    DataStore(new)  # 不得抛异常
    assert (old / "kline.parquet").exists()


def test_non_frozen_does_not_migrate(monkeypatch, tmp_path):
    exe_dir = tmp_path / "install"
    exe_dir.mkdir()
    old = exe_dir / "data"
    old.mkdir()
    (old / "kline.parquet").write_bytes(b"old")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "python"))
    new = tmp_path / "TickFlowStockPanel"

    DataStore(new)

    assert (old / "kline.parquet").exists()
    assert not (new / "kline.parquet").exists()
