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

自动化证据：

- S1/S2/S4/S5：`tests/fins/test_earnings_call_upload_adapter.py` 的真实文件系统仓储 + 上传编排；页面逐项处理与失败隔离由 `test_material_tab.py` / `test_material_tab_render.py` 覆盖。
- S3：`tests/application/test_material_upload_integration.py::test_s3_real_uploaded_material_resumes_without_upload_or_second_material`在真实快照导出点注入失败；检查同一 document_id、原件与源 meta 未改变，命令顺序 upload → process失败 → process成功，无旧页面检查点时同指纹仍跳过转换。
- S6/S7：`test_docling_upload_service_quality_gate.py` 验证纪要默认启用质量门且入库前否决；其他材料保留原默认行为。
- S8：`test_earnings_call_upload_safety.py` 与 `test_material_upload_integration.py` 验证美股/A股/港股入口在 reset 前拒绝 overwrite，原件与元数据不变。
- W1–W4：材料页单测、AppTest 与真实入库目录覆盖逐文件状态、失败单独重试、rerun不重提、公司隔离、编辑确认及持久标题/财季列表。旧记录把 W4 等同“请求构造”并不充分，本轮补上目录证据。
- W5–W7：`test_streamlit_citations.py`、`test_document_reference_service.py`、`test_streamlit_source_viewer.py` 覆盖机器引用、真实原文聚合、目录选择/刷新、全文兜底与明确身份行证据。真实上传曾暴露段落边界压平，已修正 Docling 正文读取并由混合问答回归保护。

以上是单测、Runtime 集成与无头 AppTest，不是浏览器人工点击验收，也不证明模型一定遵守引用或角色规则。

## 3. 网页层

落点：`dayu/web/streamlit/pages/`；
测试：`tests/application/test_material_tab*.py`、`test_streamlit_citations.py`、`test_streamlit_source_viewer.py`、`test_streamlit_source_routing.py`；其中路由测试调用真实 `streamlit_app.main()`，验证来源身份不被侧栏公司覆盖。

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

fixtures 目录：`tests/fixtures/earnings_call/`（项目自撰合成样本，四份内容相同，不含真实敏感材料）。

目标链路：Markdown、TXT、DOCX、文字型 PDF 各一，每种都走完网页上传 → 转换 → 预处理 → 按公司/财季搜索 → 读出 → 点击定位。
空文本或段落乱序都算失败，不接受“看起来成功”。

当前证据：`test_four_formats_real_upload_process_discover_search_read` 对四种格式走材料页执行函数 → 真实 FinsService / Runtime / Docling / 预处理 / 仓储 → list/search/read_section → 机器来源 → UI链接解析 → 真实只读来源Service。
宿主执行外壳替换为测试 Host，不调用 Agent/LLM；上传与查看器的浏览器控件另由 AppTest 覆盖，不能拼称完整浏览器 E2E。
断言保留实际42 million、分析师100 million假设及管理层否认，片段角色、身份依据和字符范围正确；无标题 MD/TXT 另验证文档级全文兜底。

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

- 结果目录 `workspace/tmp/p2-validation/`，按验证批次分别命名日志与覆盖率文件（不提交）；
- 失败分类：协议接线 / 文件解析 / 网页接线 / 资料缺口，不用新规则掩盖；
- 完成后在 `PLAN_RESPONSES_AND_TRANSCRIPTS.md` 底部追加记录（提交号、跑过的检查、未覆盖项）。

### 本轮自动化交付记录（2026-11-23，`beed209`）

| 检查 | 结果 |
|---|---|
| engine/fins/contracts/services/architecture | 4089 passed，2 skipped，4 warnings |
| application 全目录 | 1171 passed，1 warning |
| 引用 Tool Service 补验 | 95 passed（不重复计入两组总数） |
| 全仓 pyright | 0 errors / 0 warnings / 0 informations |
| 17个改动生产 Python 文件，合并 branch coverage | 逐个均≥80%；最低90.51%，最高100% |

两组共5260项通过；未声称运行整个 `tests/` 目录。日志和覆盖率在本轮 `workspace/tmp/p2-validation/` 下的 `regression-core-reviewed.log`、`regression-application-complete.log`、`regression-citation-service.log`、`pyright-complete.log`、`coverage-complete.json`，均不提交。

补充直接回归：

- `test_earnings_call_passages.py`：94例；明确行内标签只建立 unknown 边界，前序管理层/分析师/主持人身份不能泄漏，原文范围无损且不误切URL/时间。
- `test_streamlit_source_viewer.py`：21例，包含连续选择不插入额外刷新；与16例真实只读Service验证共同保护目录/URL/正文一致。
- `test_streamlit_source_routing.py`：13例真实入口，入口覆盖率93.33%；不启动替代服务器。
- `test_streamlit_chat_render.py`：18例真实AppTest及离线Service事件，聊天页覆盖率97.61%；历史/流式/终态三处都直接检查链接与会话参数。
- `test_docling_source_boundaries.py`：引擎及业务processor均保留段落；两层parser版本一并更新，旧快照需重新处理，无schema迁移。

独立复核发现的身份继承泄漏与连续目录选择失效均已修复，原反例复验通过。既有11个模型白名单失败归因后只修正测试与覆盖隔离断言，不改变默认模型。

## 7. 未完成的产品验收

- 未执行真实浏览器多文件上传、点击来源、前进/后退/刷新及真实模型答案引用验收；AppTest/Runtime 集成不替代这些步骤。
- 网页财季自动推断当前仅用文件名，标题初值也来自文件名；可逐文件编辑核对。底层标题/开场声明识别单测通过，不代表页面已经自动读取两类信息。
- 不从行内换人、跨章节姓名或没有角色标签的内容推断新身份。显式冒号行内标签只切断上一个身份的继承并标为 unknown，不做语义问答推断；无法识别的边界仍需原文核验。
- PDF仅覆盖文字型单页合成样本，不含OCR或页内高亮；质量门的顺序检查依赖可核验页序信号，不证明同页语义顺序正确。
- P3报告页仍不在本轮范围。P2自动化切片可交付，但以上未覆盖项完成之前不宣称P2全部验收通过。
