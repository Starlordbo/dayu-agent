# P2 交接文档（新会话从这里接续）

用途：不依赖旧会话上下文继续验证 P2。最近更新：2026-11-23。

## 1. 代码位置与范围

- 工作副本：`/Users/bobai/Documents/deepseek-harness/default-workspace/dayu-work`
- 分支：`feat/p2-earnings-transcripts`；远端：`https://github.com/Starlordbo/dayu-agent`
- 当前切片提交与最终检查结果见 `PLAN_RESPONSES_AND_TRANSCRIPTS.md` 末尾；先执行 `git status --short`、`git log --oneline -5` 核对。
- 旧克隆没有被本轮修改。`.venv` 是指向旧环境的符号链接，不修改环境依赖。
- **P2 尚未全量产品验收；P3 报告页不在本轮范围。**

## 2. 当前已实现且有自动化证据的切片

| 能力 | 直接证据 |
|---|---|
| 上传后必须 process 才显示本次操作可问答；处理失败只重试 process；无旧页面检查点时同指纹不重复转换 | `tests/application/test_material_upload_integration.py` 的 S3 真实快照导出失败注入 |
| 纪要默认质量门；同身份不同内容不覆盖；SEC/A股/港股在 reset 前拒绝 overwrite | `tests/fins/test_docling_upload_service_quality_gate.py`、`test_earnings_call_upload_safety.py` 与 S8 集成 |
| MD/TXT/DOCX/文字型PDF真实上传、处理、列表、搜索、读段、引用链接与只读定位 | `test_material_upload_integration.py`，合成样本在 `tests/fixtures/earnings_call/` |
| 材料页逐文件确认、内容指纹、去重控件、持久材料目录 | `test_material_tab.py`、`test_material_tab_render.py` |
| 历史/流式/终态回答中的机器来源转成安全链接；文档级不冒充章节级 | `test_streamlit_citations.py`、`test_streamlit_chat_render.py`，全文兜底 MD/TXT 集成 |
| 指定公司/文档/章节的只读原文头、目录与正文；跨公司同ID隔离；删除/未完整入库拒绝 | `test_document_reference_service.py` |
| 真实入口先路由来源再显示侧栏，刷新/连续选择不丢定位，保留聊天参数 | `test_streamlit_source_routing.py`、`test_streamlit_source_viewer.py` |
| 明确独立身份行派生角色/形式/原文范围及证据；不支持的明确行内标签切断身份继承 | `test_earnings_call_passages.py`；真实四格式问答对照 |

以上区分清楚：Runtime 集成使用测试宿主外壳；AppTest 是无头控件接线测试；**两者都不是浏览器人工验收或真实模型研究质量验证**。

## 3. 不可回退的契约

1. 仓储指纹使用 `build_upload_source_fingerprint`，包含 name、sha256、size、source，不用裸 SHA 替代。
2. 一个文件一个 `EARNINGS_CALL`，Q4 不升级为 FY；歧义需确认，不合并多季/slides/transcript。
3. 质量门在原件入库前；纪要首版禁止破坏性 overwrite，也不添加修订框架。
4. 上传 Runtime 成功是 `ok`，内部服务成功是 `uploaded`；只接受同指纹 `skipped/already_uploaded`。
5. 处理只有 `processed` 或 `skipped/version_matched` 算就绪；`ingest_incomplete` 不是成功。页面 READY 是本次操作检查点，不是持续监控外部 CLI 删改的实时状态。
6. 页面检查点键包含公司、文件名、标题/财季与内容哈希；仅在已上传后保存 document_id。持久材料目录只说明入库，不自动等同处理就绪。
7. 来源身份必须含 ticker + document_id；机器标记形如 `[[source:TICKER|DOCUMENT_ID|REF]]`，各分量规范编码。全文兜底只给两分量的文档级标记。
8. 查看器通过 Service / Tool Service / 仓储读取已存 Docling 原文，不启动 Agent、不重新转换、不从自然语言标题猜 ref。
9. 身份与问句形式独立；分析师假设不能升级为公司指引，无法证明则 unknown。Docling 正文必须保留块间/块内换行，否则独立身份行会被损坏。
10. 引擎与业务 Docling parser version 均已更新（`docling_processor_v1.2.0` / `fins_docling_processor_v1.1.0`）。业务类覆盖父类版本，不能只改父类。已有快照需重新处理；本轮没有持久化 schema 变更。

## 4. 接下来优先验证

1. **真实浏览器验收**：四格式多选上传、逐项失败与重试、来源点击、连续目录切换、刷新/返回会话及跨公司身份隔离。
2. **真实模型答案验收**：工具 citation.source_marker 原样保留，章节/文档范围与角色证据正确，不把100 million分析师假设写成公司预测。
3. **网页正文元数据预填缺口**：当前只按文件名推断财季、初填标题，用户可编辑。底层标题/开场声明规则单测不代表页面已自动提取；不得写成该能力完成。
4. **样本扩展**：目前是自撰单页文字型PDF与清晰身份行；未验收OCR、复杂真实PDF排版或跨章节身份继承。无依据维持未知，不能为了多识别而猜角色。
5. 如后续需要持续反映外部 CLI 删改/版本变化，需给页面历史 READY 检查点增加明确的只读重检契约，不能把普通 rerun 变成重复转换/处理。

不要把上述未验收项带过后直接跳到 P3。

## 5. 环境与检查命令

从工作副本运行：

```bash
source .venv/bin/activate
EDGAR_LOCAL_DATA_DIR="$PWD/workspace/tmp/edgar" HF_HOME="$PWD/.cache/hf" \
  python -m pytest tests/engine tests/fins tests/contracts tests/services \
    tests/architecture tests/application -q -p no:cacheprovider
pyright --pythonpath .venv/bin/python
```

- 缓存重定向避免第三方 Edgar/Hugging Face 默认写入工作区外；不要修改系统或虚拟环境来绕过测试配置。
- 并行覆盖率必须用独立 `COVERAGE_FILE`。使用目录目标（如 `--cov=dayu --cov-branch`），避免模块目标引发 NumPy 提前导入错误。
- 本轮旧的11个核心失败均是测试白名单漏掉已有 `openai-responses`，不是默认模型变更；已更新明确白名单并补 package/workspace 配置隔离回归。默认 thinking-plan/plan 模型断言保留。
- 自动化日志与覆盖率留在 `workspace/tmp/p2-validation/`，不提交缓存、模型数据、运行仓储或凭据。

提交使用既有作者信息与安全认证配置；不要把 token 写进 remote URL、文档或日志：

```bash
git -c user.name="Starlordbo" -c user.email="190903854+Starlordbo@users.noreply.github.com" commit -m "..."
git push origin feat/p2-earnings-transcripts
```

## 6. 协作约定

先读实际实现与测试，再修改；每个代理独占文件；整合者审查最终 diff、跑受影响测试与全仓 pyright、同步各 README 和验收记录后再提交推送。只读独立评审必须用直接复现区分缺陷与已知未覆盖项。
