"""校验四处版本号一致, 防止发布漂移 (CI 每次 PR 时执行)。

四处版本号:
  - VERSION                (带 v 前缀, 如 v0.1.88)
  - backend/pyproject.toml (PyPI 包版本)
  - backend/app/__init__.py(运行时 __version__, FastAPI 展示)
  - frontend/package.json  (npm 包版本, release.yml 发布用)
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str, extract: Callable[[Path], str]) -> str:
    path = ROOT / name
    try:
        return extract(path).strip()
    except Exception as exc:
        print(f"错误: 无法从 {name} 提取版本号: {exc}")
        sys.exit(2)


def main() -> None:
    version = _read("VERSION", lambda p: p.read_text(encoding="utf-8")).removeprefix("v")
    pyproject = _read(
        "backend/pyproject.toml",
        lambda p: tomllib.loads(p.read_text(encoding="utf-8"))["project"]["version"],
    )
    package_json = _read(
        "frontend/package.json",
        lambda p: json.loads(p.read_text(encoding="utf-8"))["version"],
    )
    init_version = _read(
        "backend/app/__init__.py",
        lambda p: re.search(r'__version__\s*=\s*"([^"]+)"', p.read_text(encoding="utf-8")).group(1),
    )

    versions = {
        "VERSION": version,
        "backend/pyproject.toml": pyproject,
        "backend/app/__init__.py": init_version,
        "frontend/package.json": package_json,
    }
    if len(set(versions.values())) != 1:
        print("版本号不一致:")
        for name, value in versions.items():
            print(f"  {name}: {value}")
        sys.exit(1)
    print(f"版本号一致: {version}")


if __name__ == "__main__":
    main()