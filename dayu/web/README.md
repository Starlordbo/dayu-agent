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
- `pages/report_tab.py`：分析报告表单、独立片段轮询与 Markdown 阅读下载。
- `report_runtime.py`：通过写作服务协议在后台线程执行，以文件锁保护启动与状态变更。
- `report_runs.py`：报告任务磁盘快照的严格解析与原子读写。


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
报告页沿用入口提供的模型清单；空选择保留各场景默认，显式选择由 WriteService 同时覆盖主写作与原有核查场景。页面刷新只读取状态，不触发写作。

`ChatTurnSubmission` 显式提供 `effective_model_name` 和 `first_chunk_timeout_seconds`。
worker 在消费事件流前将提交信息投递给 UI；UI 使用该阈值及实际模型生成首输出超时提示，
不从页面可空模型选择推断。首输出计时仍从 worker 启动开始，正文或推理的非空增量均视为首输出。
配置入口见[聊天首输出等待](../config/README.md#聊天首输出等待chat_stream_config)。
超时不会确认 Host run 的取消或终态。

### 报告执行与恢复

- 页面只向 `WriteServiceProtocol.run(WriteRequest)` 提交请求，后台线程不访问 Streamlit 上下文；Host 仍负责业务运行的生命周期与并发治理。
- 运行事实保存在 `workspace/.dayu/streamlit/report_runs/<TICKER>.json`，字段为 `state`、`pid`、`started_at`、`updated_at`、`output_dir`、`error`、`log_path`。不放入聊天状态、`st.session_state` 或进程内任务字典。
- JSON 通过同目录临时文件及 `os.replace` 原子发布。固定的 `<TICKER>.lock` 文件使用项目跨平台文件锁，串行化“检查已有任务、写 running、启动线程”；UI 竞争失败立即返回提示，不阻塞等待。锁文件保留，避免删除后产生不同 inode 的锁竞争漏洞。
- 读取 `running` 时通过进程判活接口校验 pid（POSIX 为 `os.kill(pid, 0)`）；进程消失则在锁内重新读取并落 `stale`。失败写入明确 `error`，线程启动失败同样落 `failed`，随后允许重试。
- `st.fragment(run_every=2.0)` 包含报告表单与状态，终态片段可自行恢复按钮及展示输出目录中的报告，不依赖聊天队列或其片段。阶段不可获得时显示“运行中”。
- 每次任务有独立日志，记录启动、终态和错误；不重定向进程共享 stdout/stderr，也不承诺流水线逐阶段日志。损坏状态显式报错，不静默覆盖。
- 空白联网 provider 在 WriteService 内从已解析执行配置补齐，使网页与 CLI 的断点续写签名保持同源。报告状态是新增文件格式，没有旧报告状态库或兼容读取路径。
- 后台线程随服务进程退出；重启后恢复的是任务状态，不会自动重启写作。当前仅有 pid 判活，不能区分 pid 被操作系统复用的情况。状态文件写入失败会记录服务端错误日志；磁盘不可写时无法保证终态持久化。
