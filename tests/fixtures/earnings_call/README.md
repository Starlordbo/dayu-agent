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
