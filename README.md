# AutoBill 信用卡账单汇总

**中文** · [English](README.en.md)

把分散在各家银行邮件里的信用卡账单，自动整理成一封封好看的月度进度邮件。自己部署（Docker 一条命令），数据只在你自己的服务器和邮箱里。

银行把电子账单发到一个专用邮箱，AutoBill 定时通过 IMAP **只读**拉取，用**固定规则**解析每一份账单（金额不经过 AI），和银行自己印在账单上的汇总数逐项对账，把外币折算成人民币，然后按"账单月"给你发进度邮件：哪些卡已出账、合计应还多少、钱花在哪，附上每份账单统一格式的 PDF。

> **开发进度**：第一版（M0–M8）的功能都已完成：三家银行的解析和对账、标准账单 PDF、账单月进度邮件、提醒邮件、Docker 部署。正在作者自己的服务器上做最后验证，之后发布 `v0.2.0`。路线见 [project.md](project.md#6-分期路线)。

## 支持的银行（第一版）
| 银行 | 账单格式 | 状态 |
|---|---|---|
| 中国农业银行 | HTML 邮件 | ✅ 已支持 |
| 中国建设银行 | HTML 邮件 | ✅ 已支持（消费部分待更多样本确认） |
| 中国银行 | PDF 附件 | ✅ 已支持（含多卡合并账单） |

## 做什么，不做什么
- ✅ **账单月进度邮件**：为 iPhone 自带的邮件 App 排版（深色模式），每月一个对话：已出账几张卡、合计应还、每日消费图、分类；逐笔流水折叠在邮件里，轻点展开。
- ✅ **标准账单**：每份账单一份统一格式的 HTML 和 PDF，包含全部流水，作为邮件附件。
- ✅ **逐项对账**：每份账单都和银行自己的汇总数核对，对不上会告诉你差在哪。
- ✅ **多币种**：每笔保留原币种，按账单邮件当天的汇率折算人民币。
- ✅ **提醒**：不认识的邮件、解析失败、新卡号、邮箱登录失败时发一封提醒，同一个问题只提醒一次。
- ❌ 不做还款提醒（只展示还款日），不接银行接口；金额和解析不用 AI（AI 以后只用来建议分类）。

## 部署（Docker）
服务器上只需要 Docker：

```bash
mkdir -p autobill/data && cd autobill
curl -fsSLO https://raw.githubusercontent.com/YangMingUNSW/autobill/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/YangMingUNSW/autobill/main/config.example.yaml -o data/config.yaml
curl -fsSL https://raw.githubusercontent.com/YangMingUNSW/autobill/main/autobill.env.example -o autobill.env
# 填好 data/config.yaml 和 autobill.env（邮箱密码），然后：
docker compose run --rm autobill check-mailbox
docker compose up -d
```

镜像有 x86 和 ARM 两种（服务器、NAS、苹果芯片 Mac 都能跑）。完整步骤、邮箱设置和日常命令见 [docs/deploy.md](docs/deploy.md) 和 [docs/setup.md](docs/setup.md)。

## 试一试（不用邮箱）
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
