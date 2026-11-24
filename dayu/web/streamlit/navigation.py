"""股票工作页的 URL 导航契约，与原文 source_* 身份独立。

公司只从自选股中恢复；功能页参数用于首次渲染的默认 Tab。
聊天会话仍使用各公司现有的 chat_tab_<ticker>_session 参数。
"""

from enum import StrEnum

TICKER_QUERY_KEY = "ticker"
VIEW_QUERY_KEY = "view"


class StockView(StrEnum):
    """可恢复的工作页标识；不表示原文所属公司或新建业务会话。"""

    FILINGS = "filings"
    ANALYSIS = "analysis"
    REPORTS = "reports"
    MATERIALS = "materials"
