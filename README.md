# AutoBill 信用卡账单汇总

**中文** · [English](README.en.md)

把分散在各家银行邮件里的信用卡账单，自动汇总成一份每月消费报表。自己部署，数据只留在你自己的电脑上。

银行账单邮件自动转发到一个专用邮箱，AutoBill 通过 IMAP **只读**拉取，用**固定规则**（不接 AI）解析每一份账单，和银行自己印在账单上的汇总数逐项对账，存进本地 SQLite，把外币折算成人民币，再把汇总报表发到你的邮箱。

> **开发进度**：第一版开发中。农行账单的解析、对账、本地导入和终端月报已经可以使用（里程碑 M3）；建行、中行解析器和邮件报表正在开发。路线见 [project.md](project.md#6-分期路线)。

## 支持的银行（第一版）
| 银行 | 账单格式 | 状态 |
|---|---|---|
| 中国农业银行 | HTML 邮件 | ✅ 已支持 |
| 中国建设银行 | HTML 邮件 | 开发中 |
| 中国银行 | PDF 附件 | 开发中 |

## 做什么，不做什么
- ✅ **汇总和图表**：总支出、分类、按卡分布、趋势、Top 商户；展示还款日。
- ✅ **逐项对账**：每份账单都和银行自己的汇总数核对，对不上会告诉你差在哪。
- ✅ **多币种**：每笔保留原币种，按账单邮件当天的汇率折算人民币。
- ❌ 不做还款提醒，报表不列逐笔流水，不接银行接口，不接 AI。

## 试一试（开发版）
需要 [uv](https://docs.astral.sh/uv/) 和 Git。下面用仓库自带的脱敏样本，不需要真实邮箱：

```powershell
git clone https://github.com/YangMingUNSW/autobill.git
cd autobill
$env:AUTOBILL_DATA_DIR = "$env:TEMP\autobill-dev"   # 用临时目录，不影响正式数据
uv run autobill import-dir tests/fixtures           # 导入样本账单
uv run autobill report --month 2026-08              # 在终端查看 8 月汇总
```

## 仓库里有什么
| 路径 | 内容 |
|---|---|
| [project.md](project.md) | 总览、第一版范围、已定决策、架构、风险 |
| [docs/](docs/) | 各模块设计、三家银行的账单格式规格、开发流程、操作手册 |
| [tests/fixtures/](tests/fixtures/README.md) | 作者本人的真实账单，**已脱敏**（只去掉了身份信息） |
| [CLAUDE.md](CLAUDE.md) | 给 AI 编程助手看的项目规则 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

文档用中文写；代码和注释用英文。

## 参与开发
流程、里程碑和测试规范见 [docs/development.md](docs/development.md)。

## 许可证
[MIT](LICENSE) © Larry Row
