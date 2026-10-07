# 文档

AutoBill 的设计文档都在这里，用简体中文写。总览、第一版范围和已定的决策先看 [project.md](project.md)；给 AI 编程助手的规则在 [CLAUDE.md](../CLAUDE.md)。

| 文档 | 内容 |
|---|---|
| [project.md](project.md) | 总览：定位、第一版范围、已定的决策、架构、分期、风险 |
| [development.md](development.md) | 开发流程：环境、节奏、Git、CI、里程碑、测试规范、版本发布、README 的图片 |
| [setup.md](setup.md) | 操作手册（你本人要做的）：银行电子账单、中心邮箱、转发规则、历史账单 |
| [deploy.md](deploy.md) | 部署：Docker（推荐）和 systemd、密码文件、日常命令、从备份还原 |
| [fetcher.md](fetcher.md) | 收信：邮件源、IMAP、转发和拆附件、银行识别 |
| [parsing.md](parsing.md) | 解析框架、三种定位方法、健壮性规则、通用工具、解析器测试 |
| [banks/](banks/README.md) | 银行注册表、样本覆盖矩阵，以及农行、建行、中行、工行的格式规格（写解析器之前必读） |
| [data-model.md](data-model.md) | 模型、符号约定、对账算法、汇率、SQLite 表 |
| [pipeline.md](pipeline.md) | 状态机、去重、运行层、命令行、备份 |
| [notify.md](notify.md) | 统计口径、分类规则和 AI 分类、报表时机、邮件内容、年度回顾、提醒邮件 |
| [statement.md](statement.md) | 标准账单（本地命令，可选）：设计参考、版面、PDF 生成 |
| [security.md](security.md) | 密钥、配置、数据隔离、依赖更新、新样本脱敏检查清单 |
| [research.md](research.md) | 竞品、行业趋势、评审结论、版本历史 |
| [images/](images/) | README 用的图片，由 `scripts/readme_art.py` 和 `scripts/demo_screenshots.py` 生成，数据全部是编的 |
