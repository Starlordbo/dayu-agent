# P3 报告页验收记录

用途：记录「网页分析报告 Tab」的端到端验收，区别于 P2 的证据口径。

## 2026-11-24 首次端到端（GOOGL）

- 触发方式：网页「📊 分析报告」Tab，不选模型（走各 scene manifest 默认）
- 结果：`succeeded`
- 时长：2026-11-24 16:34:51 → 17:06:34（约 31.7 分钟）
- 产物：`workspace/draft/GOOGL/`
  - `GOOGL_qual_report.md` 149 KB
  - `chapters/` 32 个 Markdown（11 章及初始/重写版本）
  - `manifest.json` 220 KB、`run_summary.json`
- 章节：11 章，失败 0

## 模型归属（取自 `manifest.json.config.scene_models`）

| scene | 模型 |
| --- | --- |
| write / regenerate / fix / repair / overview | `deepseek-v4-flash-thinking` |
| audit / confirm | `deepseek-v4-pro-thinking` |
| infer / decision | `deepseek-v4-pro-thinking` |

符合「生成用 flash、审查用 pro」的既定分工。

## 内容抽查

- 电话会议纪要：报告正文引用 12 处 `业绩电话/EARNINGS_CALL`，说明上传材料进入了写作链路。
- 显式缺口 4 处，均为「占位符 + 缺口说明 + 需要什么 + 已检索范围」格式，例如：
  - Google Cloud 剔除 TPU 系统销售后的增速与利润率（公司未披露量化拆分）
  - 高管任期与 2024–2026 岗位变动、董事会独立性、NEO 薪酬结构
- 缺口根因：本轮只下载 10-K/10-Q，未含 DEF 14A 与 8-K，因此治理与薪酬章节无法取材；属数据边界，不是写作缺陷。

## 已验收

- 网页触发 → 后台生成 → 终态落盘
- 运行状态文件 `workspace/.dayu/streamlit/report_runs/GOOGL.json` 为 `succeeded`，`started_at`/`updated_at`/`pid` 完整
- 生成产物结构与章节目录完整

## 未验证（不计入通过）

- 生成过程中刷新页面能否恢复「运行中」并继续轮询（本次未在运行中刷新）
- 报告下载按钮的实际行为
- 与 `dayu-cli write` 同参数产物的逐项对照（本轮未跑 CLI）
