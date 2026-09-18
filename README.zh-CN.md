# AutoBill

[English](README.md) · **中文**

一个自己部署的小工具：把国内信用卡的账单邮件汇总成每月的消费报表。

银行账单自动转发到一个专用邮箱；AutoBill 通过 IMAP **只读**拉取，用**纯规则（不接 AI）**解析每一份账单，和银行自己给出的汇总数对账，存进 SQLite，把外币折算成人民币，再把汇总报表发到你的邮箱。

> **状态**：早期开发中，当前是 M0（项目骨架），还不能使用。路线见 [project.md](project.md#6-分期路线)。

## 支持的银行（第一版）
| 银行 | 格式 |
|---|---|
| 中国农业银行 | HTML 邮件 |
| 中国建设银行 | HTML 邮件 |
| 中国银行 | PDF 附件 |

## 做什么 / 不做什么
- ✅ 汇总和图表：总支出、分类、按卡、趋势、Top 商户；展示还款日。
- ❌ 不做还款提醒，报表不列逐笔流水，不接银行接口，不接 AI。

## 仓库内容
| 路径 | 内容 |
|---|---|
| [project.md](project.md) | 总览、范围、决策、架构、风险 |
| [docs/](docs/) | 各模块设计文档、银行格式规格、操作手册 |
| [tests/fixtures/](tests/fixtures/README.md) | 作者本人的真实账单，**已脱敏**（去掉了身份信息） |
| [CLAUDE.md](CLAUDE.md) | 给 AI 编程助手看的项目规则 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

## 开发
需要 [uv](https://docs.astral.sh/uv/) 和 Git，详见 [docs/development.md](docs/development.md)。

## 许可证
[MIT](LICENSE) © Larry Row
