# 电话会议四格式测试样本

本目录四个 `2023Q4_call` 文件均为项目自撰合成材料，不包含真实公司研究、受保护报告或个人资料。

- Markdown / TXT：保留相同段落与明确独立身份行。
- DOCX：由相同标题、段落生成的 Word 文档。
- PDF：单页文字型 PDF；不是扫描件，也不用于 OCR 验收。

共同内容：2023 Q4，实际营收 42 million；分析师提出 next year 100 million 的假设问题，管理层明确否认发布该预测。
集成测试必须验证这一语义对照的原文仍在、身份行与段落边界仍在，不能只断言转换成功或字数大于零。

执行入口：`tests/application/test_material_upload_integration.py`。
测试经过材料页执行函数 → Service → 测试宿主外壳 → 真实 Runtime / 上传 / Docling / 预处理 / 仓储 / 搜索 / read_section，
再检查机器引用、UI 链接编码及只读 Service 定位。宿主替身不调用模型，不能代替真实 Host/Agent 对话或浏览器点击验收。
无标题 MD/TXT 全文兜底在测试临时目录内生成，确保没有章节时不能伪装成章节级引用。

四格式重建回归复用上述文件，并分别验证：

- 仅上传后材料目录可见，但处理仓储仍无快照；完成 `process_material` 后，才验证处理就绪。
- 同一 workspace 重新 `DefaultFinsRuntime.create` 并构造新的 `RuntimeSubmissionService`，通过 `list_materials`、真实来源 marker 和 `resolve_document_reference` 读取相同原文、公司头与章节。读取期间禁止 Runtime 命令，并检查没有新增宿主执行或 Agent 调用。
- 新服务同指纹上传返回 `skipped/already_uploaded`，重复处理返回 `skipped/version_matched`；材料 ID、源 meta、原件及转换产物字节保持不变，源材料和处理目录各只有一份。
- 两家公司使用同标题、同财季，明确断言得到相同 `document_id`；新服务交替读取两家公司，验证 marker 的 ticker、公司头及原件隔离。MD/TXT 仅在临时上传输入中替换一句需求描述，以不同正文验证同 ID 隔离，保留 42/100 million 与管理层否认；DOCX/PDF 使用完全相同的原始字节与不同 `company_name`，不改写二进制，也不声称已验证不同二进制正文的同 ID 隔离。

这些是无模型调用的 Runtime 集成回归，不代表浏览器 E2E 或真实模型问答质量验收。
