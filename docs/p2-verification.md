# P2 验证范围与测试清单

适用对象：本 fork 的「网页批量纪要上传及可点击原文」阶段（P2）。
上游计划：[PLAN_RESPONSES_AND_TRANSCRIPTS.md](../PLAN_RESPONSES_AND_TRANSCRIPTS.md) 的 §4 P2 与 §5。

本文件是 P2 的验收基线：实现前先写测试，验收时逐项打勾；每项通过都要能指到一条测试名或一次真实操作记录。

## 0. 范围与边界

做：

- 公司资料页多文件上传区，一个文件 = 一个材料，登记为 `EARNINGS_CALL`；
- 财季识别来自文件名、文档标题、开场声明；
- 逐文件状态：上传中 / 处理中 / 失败 / 可问答；
- 引用接线：文档编号 + 章节定位落到真实原文查看入口。

不做（首版明确排除）：

- 扫描件 OCR、旧 `.doc`；
- 自动覆盖、自动判定修订版本、版本管理框架；
- 把多季纪要合成一个材料包；
- 新增审核步骤或审核模型。

## 1. 单元层

落点：`dayu/fins/upload_recognition.py`（`infer_earnings_call_fiscal`）；
测试：`tests/fins/test_earnings_call_fiscal_recognition.py`。

| # | 用例 | 期望 |
|---|---|---|
| U1 | 文件名含 `Q4 2023` | `fiscal_year=2023, fiscal_period=Q4` |
| U2 | 标题 "Fourth Quarter and Full Year 2023 Results" | 保留 Q4，不升级成 FY |
| U3 | 标题 "Q4 and Full Year 2023 Earnings Call" | 一个财季判断（Q4），reason 标记 q4_and_fy_single_material |
| U4 | 文件名 `FY2022` + 开场提 2023 展望 | 仍为 FY2022，展望不改变归属 |
| U5 | 财年截至 1 月（标题写 Fiscal Year Ended January 31, 2023） | 取 2023，不从发布日 2024 推断 |
| U6 | 文件名/标题/开场均无财季声明 | `needs_confirmation=True`，不给猜测结果 |
| U7 | 文件名 Q3 2023，标题 Q4 2023 | `conflict=True`，取更高优先级来源（标题）并提示确认 |
| U8 | 同一电话会的 slides 与 transcript 两个文件 | 分别登记为独立材料，不合并（服务层验证） |
| U9 | 开场 "Q4 2023 results compared with Q4 2022" | 年份取 2023，不被对比年份干扰 |
| U10 | 同一标题出现 "Q4 2023 and Q2 2024" | 判为冲突并要求确认，不静默取首个 |
| U11 | "Full Year 2023 with Q4 details" | 主口径为 FY（最先出现的声明） |
| U12 | 中文对比句 "与2022年Q4相比，2023年Q4收入增长" | 取 2023 Q4 |
| U13 | 前置对比从句 "Compared with 2022, Q4 2023 revenue grew" | 只删对比分句，仍取 2023 Q4 |
| U14 | 文件名含发布日 `2024-02-01_Q4_2023.txt` + 标题 Q4 2023 | 不误报冲突，取 2023 |
| U15 | 跨公司引用 "乙公司 2022；甲公司 Q4 2023" | 年份按最近取 2023 |

## 2. 服务 / 管线层

落点：`docling_upload_service` / `sec_pipeline` 的批处理编排；
测试：`tests/fins/test_earnings_call_batch_upload.py`（新增），扩展现有
`tests/fins/test_docling_upload_service.py`、`tests/fins/test_sec_pipeline_upload_material_stream.py`。

| # | 用例 | 期望 |
|---|---|---|
| S1 | 3 个文件，第 2 个转换失败 | 其余成功可问答；失败项单独标记；整批不中断 |
| S2 | 同指纹重复上传（已成功） | 复用既有材料，不重复产生 |
| S3 | 上传成功、处理失败后重试 | 只恢复处理，不产生第二份材料 |
| S4 | 同名同季、内容不同 | 冲突可见，旧件保留，新件另存独立材料 |
| S5 | 同季不同电话会（不同公司同名季） | 不自动判为修订 |
| S6 | 转换成功但正文为空 | 判为不可问答 |
| S7 | 段落顺序损坏 | 判为不可问答并说明原因 |
| S8 | 调用带 overwrite 的老路径 | 首版拒绝破坏性覆盖 |

进度说明：S1、S2、S4、S5 已由 `tests/fins/test_earnings_call_upload_adapter.py`
通过真实文件系统仓储 + 既有上传服务验证；S6、S7 在服务层验证
（`tests/fins/test_docling_upload_service_quality_gate.py`：转换结果不可问答时入库前否决）；
S3 目前只验证到决策层（resume → update），真实“上传成功但处理失败”的部分状态
需要在网页接线时用可注入的失败点复测。

网页层：W1（逐文件条目）、W2（结果映射与失败展示）、W3（批次指纹去重）、W4（upload_material 请求构造）
已由 `tests/application/test_material_tab.py` 覆盖；W5–W7（引用点击定位、文档/章节标注、分析师与管理层区分）
依赖原文查看入口，尚未接线；真实浏览器交互未验收。

## 3. 网页层

落点：`dayu/web/streamlit/pages/`；
测试：`tests/application/test_upload_page_*.py`（新增，仿 `test_streamlit_chat_stream_runtime.py` 的无头断言）。

| # | 用例 | 期望 |
|---|---|---|
| W1 | 一次选 4 个文件 | 4 行独立进度，不是单一总进度 |
| W2 | 批次中途一项失败 | 其余继续，失败项可单独重试 |
| W3 | 页面重绘（rerun） | 不重复上传/处理，不重复计费 |
| W4 | 处理完成 | 材料出现在按公司/财季列表，标题为真实标题 |
| W5 | 点击引用 | 跳到原文对应章节，带公司/财季/标题 |
| W6 | 文档级 vs 章节级引用 | 分别标示，不能只给编号 |
| W7 | 分析师提问 vs 管理层回应 | 引用区能区分来源段落 |

## 4. 真实格式端到端

fixtures 目录：`tests/fixtures/earnings_call/`（脱敏副本，不含真实敏感材料）。

Markdown、TXT、DOCX、文字型 PDF 各一，每种都走完：网页上传 → 转换 → 预处理 → 按公司/财季搜索 → 读出 → 点击定位。
空文本或段落乱序都算失败，不接受“看起来成功”。

## 5. 回归

P1 相关与既有上传测试不得新增失败：

```bash
.venv/bin/python -m pytest tests/engine/test_responses_protocol.py \
  tests/application/test_responses_service.py tests/engine/test_sse_parser.py \
  tests/fins/test_docling_upload_service.py tests/fins/test_sec_pipeline_upload_material_stream.py \
  tests/application/test_streamlit_chat_stream_runtime.py -q
```

基线：P1 相关 96 passed。

## 6. 通过标准与证据

- 结果目录 `workspace/tmp/p2-validation/<timestamp>/`（不提交）；
- 失败分类：协议接线 / 文件解析 / 网页接线 / 资料缺口，不用新规则掩盖；
- 完成后在 `PLAN_RESPONSES_AND_TRANSCRIPTS.md` 底部追加记录（提交号、跑过的检查、未覆盖项）。
