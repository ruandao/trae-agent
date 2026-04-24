"""在导入业务代码前校验解释器版本，避免 3.12 以下因 ``match`` 等语法在收集阶段即失败。"""

from __future__ import annotations

import sys

_MIN = (3, 12)

if sys.version_info < _MIN:
    v = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    msg = (
        f"当前 Python 为 {v}，本仓库要求 >= {_MIN[0]}.{_MIN[1]} "
        f"（见 pyproject.toml: requires-python）。\n"
        "请使用与 .python-version 一致的解释器，例如：\n"
        "  uv run --python 3.12 python -m pytest\n"
        "  或: make test\n"
        "  或: python3.12 -m pytest"
    )
    raise RuntimeError(msg)
