"""工具治理提示的信封边界测试。"""

import pytest

from dayu.contracts.agent_types import JsonValue
from dayu.engine.tool_result import get_user_notice


@pytest.mark.parametrize(("result", "expected"), [
    ({"ok": True, "meta": {"user_notice": " 提示 "}}, "提示"),
    ({"ok": False, "meta": {"user_notice": "失败"}}, None),
    ({"ok": True}, None),
    ({"ok": True, "meta": "invalid"}, None),
    ({"ok": True, "meta": {"user_notice": " "}}, None),
    ({"ok": True, "meta": {"user_notice": 1}}, None),
])
def test_get_user_notice(result: dict[str, JsonValue], expected: str | None) -> None:
    """只接受成功信封中的非空提示。

    参数:
        result: 合法或非法提示信封。
        expected: 预期提示。
    返回值:
        无。
    异常:
        AssertionError: 提示读取越过契约。
    """
    assert get_user_notice(result) == expected
