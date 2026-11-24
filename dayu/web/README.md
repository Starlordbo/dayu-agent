## `dayu.web` 开发说明

本文档说明 `dayu.web` 的当前实现边界，重点覆盖 Streamlit 模块的目录职责、聊天链路与稳定约束。

## 1. 模块定位与入口

- `dayu.web` 是 UI 适配层：把页面/HTTP 请求收口为 `ServiceProtocol` 调用，再把结果渲染或序列化返回。
- 稳定分层保持为 `UI -> Service -> Host -> Agent`，UI 不直接触碰 Host 内部实现细节。
- 当前有两条 Web 入口：
  - Streamlit UI：`dayu/web/streamlit_app.py`
  - FastAPI API：`dayu/web/fastapi_app.py`

| 入口 | 目标用户 | 负责 | 不负责 |
| --- | --- | --- | --- |
| `dayu/web/streamlit_app.py` | 人工交互用户（浏览器） | 页面渲染、`st.session_state`、Service 注入、Tab 组合 | HTTP API 契约与路由 schema |
| `dayu/web/fastapi_app.py` | 程序化调用方（HTTP 客户端/Worker） | API 装配、路由注册、请求/响应契约、后台任务受理 | Streamlit 页面状态与组件交互 |

## 2. Streamlit 模块结构

`streamlit_app.py` 的职责边界：

- 解析 workspace（优先 `DAYU_WORKSPACE`，否则 `<cwd>/workspace`）。
- 只在启动阶段装配一次 Host runtime，并在 `st.session_state` 缓存 Service 实例。
- 调用 `render_sidebar()` 与 `render_stock_detail_page()`，不把业务逻辑塞入入口文件。

`dayu/web/streamlit/` 当前按“组件 + 页面”拆分：

- `components/`：边栏与通用组件（如自选股列表）。
- `pages/main_page.py`：主功能区入口，组合四大 Tab（财报管理、交互式分析、分析报告、会议纪要）。
- `pages/filing_tab.py`：财报下载与处理页面。
- `pages/chat_tab.py`：交互式分析页面编排（渲染、输入、历史加载、清空会话、流式轮询）。
- `pages/report_tab.py`：分析报告页面。


## 3. 开发约束与测试锚点

- Streamlit/FastAPI 都必须通过 `ServiceProtocol` 访问业务能力，不绕过 Service 直连 Host 内部对象。
- 聊天历史读取与清空走 `ChatServiceProtocol` 的公开方法，不在 UI 侧直接读写 archive。
- 流式轮询进入终态后先在页面会话状态中保存反馈，再清理运行时并整页刷新，让片段外的提交按钮恢复可用。执行错误、空回复、警告与内容过滤提示会在对话区重绘；失败或空回复保留输入，成功回复清空输入。反馈仅用于当前会话，下一次有效提交、成功清空或新建会话时清除，不写入后端历史。
- 无显式会话参数时使用公司级默认键（`streamlit-web-{COMPANY_ID}`）；URL 中的 `chat_tab_<ticker>_session` 优先恢复原会话，即使当前可选模型目录为空也不退回公司默认会话。
- 自选股持久化边界由 `tests/application/test_streamlit_watchlist.py` 守护。

### 会议纪要上传

- 入口：`pages/material_tab.py`，主功能区第四个 Tab。
- 一个文件登记为一份材料：逐文件推断财季，并允许编辑标题、财年、财季。上传命令携带自选股公司名称，随后提交 `process_material`；两步均 `stream=False`。
- 只有处理成功或 `skipped/version_matched` 才展示“可问答”。上传成功只表示原件及转换结果入库，不表示已有查询快照。
- 单文件失败不影响同批其他文件；检查点键包含公司、文件名、标题、财季与内容 SHA-256，同名等长但内容不同不会误复用。
- 会话中保留上传成功的 document_id，处理失败可单独重试且只提交处理命令。会话检查点丢失后，重新提交由仓储同指纹跳过转换，再恢复或复用处理快照。
- 财季无法确认时须逐文件补全并核对；不猜测缺失财年/财季。冲突上传保留旧件，用户修改标题后另存，不提供破坏性覆盖。
- 暂存文件按内容摘要隔离，保留安全的原始 basename，拒绝带路径分隔符的文件名。完全相同的重复选件只显示一次控件。
- 已入库目录通过 `FinsService.list_materials` 按公司读取仓储标题/财季，刷新和清空选件后仍可访问原文；目录不等同“可问答”状态。
- 选文件时，页面将名称与字节交给 Service 预填：MD/TXT/DOCX 轻量提取标题与开头两段，财季按开场声明 > 标题 > 文件名推断。标题初值优先采用正文标题；提取失败时退回文件名 stem。正文来源、文件名来源和未知财季分别提示，冲突或信息不完整须确认，用户始终可编辑覆盖。
- PDF 在预填阶段不提取正文，仅按文件名推断并提示人工核对；不支持预填阶段的 OCR。选文件不调用 Docling，不提交上传或处理命令。

### 原文查看与引用

- 工具 citation 的 `source_marker` 格式为 `[[source:TICKER|DOCUMENT_ID]]` 或带第三个 `REF` 分量的章节引用，各分量规范 URL 编码。
- `pages/chat/citations.py` 只处理完整合法标记，不处理代码块、行内代码、reasoning 或用户文本；历史及流式累计正文共用转换。无定位的旧回答保留普通文本，不做模糊反查。
- 链接使用 `source_ticker`、`source_document`、可选 `source_ref` 查询参数，保留非 `source_` 参数（包括聊天 session）。
  `streamlit_app.py` 在侧栏之前路由至 `pages/source_viewer.py`，始终使用链接中的公司身份，刷新可恢复。
- 返回上下文与来源身份分离：聊天/材料目录链接显式写入原公司 `ticker` 和 `view=analysis` / `view=materials`，不会从来源公司的 `source_ticker` 猜测原公司。查看器“返回来源页面”只移除 `source_*`，保留原公司、功能页、公司专属会话及其他参数。
- 侧栏每次从持久自选股核对 `ticker`；合法 URL 优先于旧页面选择，失效或已删除目标不自动退回旧公司/来源公司，用户可重新选股。主页面按 `view` 设置默认 Tab，未知值回到财报管理；这里不是手动切 Tab 后实时同步 URL 的机制。
- 查看器通过只读 Service 展示公司、财季、真实材料标题、目录及完整章节正文；无细粒度定位显示文档级，全文伪章节显示“文档级 / 全文”。
- 纪要显示发言片段的字符范围、明确身份依据及问句/陈述形式；分析师提问与管理层陈述不会共用整章身份标签，未知显示“未标注”。
- 这是已入库转换正文的章节查看器，不是原始 PDF 版面/页内高亮或原件下载接口。只读访问不提交聊天、上传或处理命令。

### 对话模型选择

Streamlit 交互页只展示已配置且在 interactive 场景允许名单内的模型。
模型选择经线程 worker 的 `ExecutionOptions` 传给 ChatService；服务端 session
元数据是续聊模型的真源，页面默认值不会覆盖它。“新建会话”将独立 ID 写入 URL，
刷新可恢复同一会话；旧会话历史不会被删除。生成中禁用新建动作。
报告页仍为占位实现，不会因页面刷新触发报告或模型请求。
