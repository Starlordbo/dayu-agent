# P2 交接文档（新会话从这里接续）

用途：新会话（默认模型 `openai/gpt-6-astra`，带多模型子代理路由）在不依赖旧会话上下文的情况下继续 P2。
最近更新：2026-11-23。

## 1. 代码位置

- 工作副本：`/Users/bobai/Documents/deepseek-harness/default-workspace/dayu-work`（会话 workspace 内，提交/推送无需审批）
- 分支：`feat/p2-earnings-transcripts`（基于 `feat/responses-p1`，P1 已在其中）
- 远端：`https://github.com/Starlordbo/dayu-agent`
- 基线提交：见 `git log --oneline -5`
- 原克隆 `/Users/bobai/Documents/Codex/2026-11-23/.../dayu-agent` 未被改动，需要时 `git pull`

## 2. 已完成的切片

| 提交 | 内容 |
|---|---|
| `b8d4120` | 批量调度接真实 `DoclingUploadService`；暴露 `build_upload_source_fingerprint` |
| `3dbf3c6` | 转换质量判定 `assess_docling_payload`（空正文/乱码/页序回退） |
| `4dd5922` | 质量门接入 `DoclingUploadService(assess_docling=...)`，入库前否决 |
| `725b8fa` | Streamlit「📞 会议纪要」Tab：多文件、逐文件状态、批次去重 |
| `578ca1c` / `129aff8` | 计划文档与验收进度留痕 |

验收项覆盖（详见 `docs/p2-verification.md`）：U1–U15、S1、S2、S4、S5、S6、W1–W4。

## 3. 关键设计决策（改之前先读，避免回退）

1. **指纹必须用 `build_upload_source_fingerprint`**
   服务内部指纹是 `name+sha256+size+source` 的结构化哈希；自算 sha256 会与仓储 `source_fingerprint` 格式不一致，把每次重传都判成冲突。
2. **EARNINGS_CALL 的 Q4 不升级为 FY**
   `infer_earnings_call_fiscal` 取“最先出现的声明”定主口径：`Q4 and Full Year 2023` → Q4，`Full Year 2023 with Q4 details` → FY；同源出现两个不同季度判冲突并要求确认。
3. **质量门在 upsert 前**
   转换成功但空正文/乱码/页序回退 → `RuntimeError` 让该文件失败，不留 `ingest_complete=True` 的坏材料。
4. **网页上传用同步结果**
   `upload_material` 以 `stream=False` 提交，逐文件立即得状态；页面用批次指纹（文件名+字节数）去重，重绘不重复提交。
5. **一个文件 = 一个材料**
   不把多季或 slides/transcript 合并成一个材料包。

## 4. 未完成项（按建议顺序）

1. **S3 真实部分状态**：给上传编排加可注入失败点，验证“上传成功但处理失败 → 只恢复处理”。
2. **四格式真实端到端**：md / txt / docx / 文字型 PDF 各一份，走完上传→转换→搜索→读取→定位。
   - 已实测（真实 Docling，本机 mps）：`.md` 3 items / 74 chars、`.txt` 3 items / 74 chars、`.docx` 3 items / 74 chars、`.pdf` 6 items / 274 chars，四种全部 `ready`。
   - 结论：Docling 接受全部四种格式，端到端验收不需要额外格式适配；fixtures 可用 `python-docx` 生成 docx、文本写 md/txt、PDF 复用 `tests/fixtures/docling/dayu_docling_integration_fixture.pdf`。
3. **W5–W7 引用定位**：引用点击跳到原文对应章节、文档级与章节级分别标注、分析师提问与管理层回应可区分；需先读现有原文查看入口。
4. **P3 报告页**：`report_tab` 占位换成真实 `WriteService`。
5. 扩大回归里既有的 13 failed + 2 errors（P1 记录）仍需逐项归因。

## 5. 环境与命令

```bash
cd /Users/bobai/Documents/deepseek-harness/default-workspace/dayu-work
# 测试（必须带 EDGAR 与 HF 缓存重定向，否则会被沙箱挡住）
EDGAR_LOCAL_DATA_DIR="$PWD/workspace/tmp/edgar" HF_HOME="$PWD/.cache/hf" \
  .venv/bin/python -m pytest tests/fins/test_earnings_call_*.py tests/application/test_material_tab.py -q -p no:cacheprovider
# 类型检查（pyrightconfig 排除了 .venv，需显式给 pythonpath）
.venv/bin/pyright --pythonpath .venv/bin/python <files>
```

提交与推送（工作副本在 workspace 内，无需审批；push 用凭据库里的 `GITHUB_TOKEN`）：

```bash
git -c user.name="Starlordbo" -c user.email="190903854+Starlordbo@users.noreply.github.com" commit -m "..."
TOK=$(python3 -c "import re,pathlib;print(re.search(r'^\s*GITHUB_TOKEN\s*:\s*(\S+)', pathlib.Path.home().joinpath('.dsh/.credentials.yaml').read_text(), re.M).group(1))")
git -c credential.helper= -c http.extraheader="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$TOK" | base64)" push origin feat/p2-earnings-transcripts
```

## 6. 协作约定

- 每次改动：先读现有实现与测试 → 改 → 跑受影响测试 → pyright → 同步 `dayu/fins/README.md` 或 `dayu/web/README.md` → 在 `PLAN_RESPONSES_AND_TRANSCRIPTS.md` 底部追加记录 → 提交推送
- 独立评审可派 `subagent_codex`（一次性、自包含 prompt，只读评审）
- 只有真正的产品取舍（交互形态、默认模型、数据缺口处理）才停下来问用户
