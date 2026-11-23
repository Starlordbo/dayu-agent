"""Fins 管道内描述结构化 JSON 载荷的类型别名。

用递归联合类型代替 `Any`/`object`，让载荷解析保持可检查：
`JsonValue` 覆盖 JSON 的标量、列表与对象，调用方按需 `isinstance` 收窄。
"""

from __future__ import annotations

from typing import Union

# JSON 标量。
JsonScalar = Union[str, int, float, bool, None]

# JSON 值：标量、列表或对象，递归定义。
JsonValue = Union[JsonScalar, list["JsonValue"], dict[str, "JsonValue"]]
