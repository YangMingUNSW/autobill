# AutoBill：基于邮箱转发与规则解析的信用卡账单汇总工具

> 版本 v3.2（2026-09-19）：补上开发流程（[docs/development.md](docs/development.md)）和给 AI 助手看的规则（[CLAUDE.md](CLAUDE.md)），并划清第一版的范围。本文件是**总览和入口**；各主题的细节只写在 `docs/` 下对应的文件里，这里只放摘要和链接。版本历史见 [docs/research.md](docs/research.md#版本历史)。

## 1. 定位与目标
- **痛点**：多张信用卡的账单分散在各家银行发来的邮件里，格式各不相同（农行、建行是 HTML，中行是 PDF 附件），缺一个统一的汇总分析工具。
- **定位**：自用优先、轻量的开源数据管道，**公开仓库**。
  - 不碰金融 App，不抓银行接口，只需要一个专用邮箱的授权。
  - **不接入 AI**，全部用确定性规则解析。
  - **只做汇总分析，并展示还款日**；不做还款提醒，也不追踪是否已还款。
- **手机端**：不做 App，在手机上看邮件报表。报表**只做汇总和可视化**，逐笔明细去看银行原账单。
- **竞品**：51信用卡管家等商业产品要把邮箱授权交给第三方；开源项目要么只有汇总，要么需要手动导出。详见 [docs/research.md](docs/research.md)。

## 2. 第一版（MVP）范围
先做一个**能用的最小版本**，用起来之后再决定要不要加别的。设计文档里"以后"的内容都保留着，只是不在第一版里做。

| 第一版做 | 以后再说 |
|---|---|
| 解析农行、建行、中行 | 兜底解析器（第一版里解析失败就直接告警） |
| 分项对账，含合成调整流水 | 质量优先级（`quality`） |
| SQLite，用唯一键保证导入幂等 | outbox、账单更正推送（第一版用 `reported_at` 代替） |
| 汇率：Frankfurter + 缓存 + 兜底汇率 | RETRY/DEAD 退避重试 |
| 分类规则 | DKIM 来源校验 |
| 报表：先终端输出，再发邮件加图表；每收到一份账单发一封 | 缺账单心跳 |
| IMAP 只读游标拉取，含拆附件和银行识别 | 企业微信、Oracle 中转和看门狗 |
| Windows 计划任务，出错时发告警邮件 | Beancount 导出、Fava 查账网页 |
| | Docker 打包（可选，第一版跑通后再做；代码尽量不依赖 Windows 特有功能） |

第一版拆成 **M0–M8 九个小步骤**，每一步都写明了做完的标准和怎么验证，见 [docs/development.md §5](docs/development.md#5-里程碑第一版m0m8)。

## 3. 已拍板的决策
| 决策 | 内容 |
|---|---|
| 解析方式 | 纯规则，不接 AI；`BaseParser` 接口本身留有扩展余地 |
| 邮件来源 | 主邮箱按银行发件人自动转发到中心邮箱；历史账单在网页里分小批"作为附件"转发；只回填最近 12 个月。**这一点确认过两次，不改成直接读主邮箱** |
| 报表内容 | **只做汇总和可视化**：总支出、分类、按卡分布、趋势、Top 商户；**不列逐笔流水**；只展示还款日，**不做提醒** |
| 报表时机 | **每收到一份账单发一封**：本期摘要 + 涉及月份的最新汇总 + 标注还缺哪些卡。没有定时月报；`report --month` 可以随时手动查看 |
| 币种 | 每笔记原币种；总支出 = 账单上各币种入账金额分别相加，再按**账单邮件当天的网上汇率**（Frankfurter）折算成人民币，不追求精确 |
| 推送渠道 | 第一版只有邮件；企业微信以后再说（只推摘要、只推本人），不用第三方推送服务 |
| 部署 | 家里的 Windows 电脑，用计划任务定时运行；第一版不用 Docker（Docker 打包列入"以后"，2026-09-19 定）。以后加企业微信时，数据仍然留在家里，Oracle 只做中转 |
| 银行范围 | 农行（HTML）、建行（HTML）、中行（PDF） |
| 公开仓库 | 整个项目公开；测试样本就是作者本人的真实账单，**一次性脱敏**（没有脱敏脚本） |
| 隐私边界 | 只清理身份信息：姓名、住址、卡号、证件号、手机号、邮箱/QQ；消费记录、金额、商户、额度保持原样 |
| 开发流程 | 每一步开一个分支，一步一个 PR，CI 全绿，由作者自己合并；详见 [development.md](docs/development.md) |

## 4. 架构（标 ⏳ 的是"以后"）

```text
[银行账单邮件] ──自动转发──┐        ┌──批量"作为附件"转发── [历史账单]
                           ▼        ▼
            [中心账单邮箱：收件箱 / CreditCard_Raw / 垃圾箱]
                           │ IMAP 只读：EXAMINE + BODY.PEEK[]，UID 游标
                           ▼
 Fetcher     原件按 sha256 落盘 · 拆出 message/rfc822 · PDF 按内容识别
             · 银行识别（发件人 + 主题 + 正文特征）· ⏳ 按银行配置 DKIM 校验
                           ▼
 Parser      农行：顺序状态机 · 建行：锚点子表 · 中行：PDF 标签加坐标
             · 解析失败 → 告警（⏳ 兜底启发式抽取）
                           ▼
 Reconcile   分项对账（借方 / 贷方 / 汇总恒等式）· 未列明细的调整 → 合成流水
                           ▼
 Store       SQLite（WAL）：一个事务里完成 upsert 账单和替换流水 · fx_rates 汇率缓存
                           ▼
 Report      汇总 + 图表 → 邮件（每份账单一封，发送成功记下 reported_at）
             ⏳ 企业微信摘要 → WireGuard → Oracle 中转 + 心跳看门狗
```

## 5. 文档导航与代码包对照

| 文档 | 内容 | 代码包 |
|---|---|---|
| [CLAUDE.md](CLAUDE.md) | **给 AI 助手看的项目规则和红线**，每次开新对话先让它读 | — |
| [docs/development.md](docs/development.md) | **开发流程**：环境、节奏、Git、CI、里程碑 M0–M8、测试规范、版本发布、词汇表 | — |
| [docs/fetcher.md](docs/fetcher.md) | 邮件源抽象、IMAP、转发和拆附件、银行识别、⏳ 来源校验 | `autobill/fetch/` |
| [docs/parsing.md](docs/parsing.md) | 解析框架、三种定位方法、健壮性规则、通用工具、解析器测试 | `autobill/parse/`（`base`、`registry`、`util`） |
| [docs/banks/](docs/banks/README.md) | 银行注册表、样本覆盖矩阵，以及[农行](docs/banks/abc.md)、[建行](docs/banks/ccb.md)、[中行](docs/banks/boc.md)的格式规格 | `autobill/parse/{abc,ccb,boc}.py` |
| [docs/data-model.md](docs/data-model.md) | 模型、符号约定、对账算法、汇率、SQLite 表、Beancount 映射 | `autobill/model.py`、`autobill/reconcile.py`、`autobill/fx.py` |
| [docs/pipeline.md](docs/pipeline.md) | 状态机、去重、运行层、CLI、备份、部署、技术栈 | `autobill/pipeline.py`、`autobill/store/`、`autobill/cli.py` |
| [docs/notify.md](docs/notify.md) | 统计口径、分类规则、报表时机和内容、邮件、⏳ 企业微信 | `autobill/report/`、`autobill/notify/` |
| [docs/security.md](docs/security.md) | 密钥、配置示例、数据隔离、.gitignore/.gitattributes/pre-commit、新样本脱敏清单 | `autobill/config.py` |
| [docs/setup.md](docs/setup.md) | **操作手册**（你本人要做的）：银行电子账单、中心邮箱、转发规则、历史账单、计划任务 | — |
| [docs/research.md](docs/research.md) | 竞品、行业趋势、v1 评审结论、版本历史 | — |

**测试样本**在 [`tests/fixtures/`](tests/fixtures/README.md)：脱敏后的真实账单，随仓库公开。原件不在仓库里。

## 6. 分期路线

| 阶段 | 内容 | 验收 |
|:---|:---|:---|
| **第一版（当前）** | M0 骨架 → M1 模型和工具函数 → M2 农行解析器 → M3 第一条完整链路 → M4 建行 → M5 中行（`v0.1.0`）→ M6 分类和报表 → M7 邮件报表 → M8 IMAP 和计划任务（`v0.2.0`） | 每一步的标准见 [development.md §5](docs/development.md#5-里程碑第一版m0m8)；最终标准是一封真实账单从转发到收到报表全程自动完成 |
| **补样本（P1b）** | 穿插在第一版中间进行 | 每家银行 ≥3 期，覆盖[样本覆盖矩阵](docs/banks/README.md#样本覆盖矩阵)里的主要场景，"推断"全部改成"已验证" |
| **以后（按需）** | 第 2 节"以后再说"那一列 | 用过第一版之后再决定 |

**已删除**：v1 里的"接入 LLM"和"MCP 接口"两个阶段。

## 7. 待定事项
- [ ] 中心邮箱用哪家服务商，是新开一个 QQ 小号还是 163？（M8 之前定下来即可）
- [ ] **补样本**：~~有消费的建行账单；有明细和应还金额的中行账单~~（2026-09-19 已补）；每张卡再补 1–2 期；退款、年费、取现。缺口详情见[样本覆盖矩阵](docs/banks/README.md#样本覆盖矩阵)。
- [ ] 主邮箱的自动转发规则怎么设置，以及转发后原邮件会不会被改动（M8 用真实转发件验证）。
- [ ] 逐张卡检查电子账单投递设置：开通邮件账单，并且带明细。
- [x] **git init 和 GitHub 公开仓库**（M0 的第一件事，单独确认）：README（**中文为主** `README.md`，英文 `README.en.md`；2026-09-19 改为中文优先，面向中国大陆用户）、LICENSE（MIT © Larry Row）、CHANGELOG.md；首次推送前再全文搜一遍身份信息。
- [x] ~~中行 PDF 的密码~~：实测未加密。
- [x] ~~关键金额是不是图片~~：三家都是文字。

## 8. 风险清单（⏳ 表示对策属于"以后"）

| # | 风险 | 对策 | 详见 |
|---|---|---|---|
| A1 | 程序没在运行时，本机没法告警自己 | 第一版接受：最坏的结果只是某次没收到报表；⏳ Oracle 看门狗 | [pipeline](docs/pipeline.md#运行层) |
| A2 | ⏳ 零消费月银行不出账单，缺账单心跳误报 | 按银行配置零账单行为（中行实测会发） | [parsing](docs/parsing.md#缺账单心跳) |
| A3 | ⏳ 兜底解析抽错金额 | 标注"未核实，以原邮件为准"。第一版没有兜底，解析失败就直接告警 | [parsing](docs/parsing.md#兜底解析器) |
| A4 | ⏳ 低质量解析覆盖高质量解析 | 按 `quality` 判断。第一版没有兜底，所有账单质量相同，不存在这个问题 | [pipeline](docs/pipeline.md#三层去重) |
| A5 | 汇总块不全，无法对账 | `UNVERIFIED` 状态 | [data-model](docs/data-model.md#对账) |
| A6 | 伪造账单邮件 | 中心邮箱地址保密、报表不放链接；⏳ 按银行配置 DKIM | [fetcher](docs/fetcher.md#来源校验) |
| A8 | **未列明细的调整**（农行银联卡 0.62）导致误报 | 分项对账 + 合成调整流水 | [data-model](docs/data-model.md#对账) |
| A9 | 建行没有 DKIM，无法验证来源 | ⏳ 做 DKIM 校验时，建行跳过，接受残余风险 | [fetcher](docs/fetcher.md#来源校验) |
| A10 | **建行在没有流水的月份拿不到卡号** | 回退到 `CCB:unknown` 并标 WARN（M4）；按单卡配置直接指定放到 M8，再人工映射 | [banks/ccb](docs/banks/ccb.md) |
| A11 | 国内微信、支付宝消费看不到真正的商户 | 账单本身的限制，归入"微信/支付宝（未细分）" | [notify](docs/notify.md#分类规则) |
| B1 | 交易日没有年份 | 三家都带年份；推断逻辑留给以后接入的银行 | [parsing](docs/parsing.md#通用工具) |
| B2 | 字符集标错导致乱码 | 失败时用 GB18030 兜底 | [fetcher](docs/fetcher.md#mime-处理) |
| B3 | 金额格式五花八门 | 统一的金额解析函数（M1），每种格式都有测试 | [parsing](docs/parsing.md#通用工具) |
| B4 | 同一封邮件里有两套符号约定（农行） | 按字段分别处理，以汇总块为准 | [banks/abc](docs/banks/abc.md) |
| B5 | 中行伪粗体造成重复字符 | 用 `dedupe_chars()`，只认中文标签 | [banks/boc](docs/banks/boc.md) |
| B6 | 历史模板有多个版本 | 只回填 12 个月 | [fetcher](docs/fetcher.md) |
| C1 | 计划任务读不到授权码，或者休眠时错过运行 | 设为"仅登录时运行"并勾选"错过后尽快运行" | [setup](docs/setup.md#5-本机运行m8) |
| C2 | 批量转发变成会过期的超大附件链接 | 分小批转发 | [setup](docs/setup.md#4-历史账单只做一次) |
| C3 | 垃圾箱被自动清理，原件丢失 | 收信规则把银行邮件移到专属文件夹 | [setup](docs/setup.md#2-新开中心邮箱) |
| C5 | 换卡后尾号变化 | `card_aliases` 映射 | [security 配置示例](docs/security.md) |
| C6 | 测试误碰真实数据库 | 测试一律用 `tmp_path` 作为 `AUTOBILL_DATA_DIR` | [development](docs/development.md#6-测试规范) |
| C7 | **加入新样本时泄露身份信息** | 按检查清单逐项查（五个方面 + 通用规则扫描）；CI 里也有身份扫描 | [security](docs/security.md#以后加入新样本时的检查清单) |
| C8 | Git 的 `core.autocrlf` 改写样本的换行 | `.gitattributes`：`*.eml -text`、`*.pdf binary`（已实测有效） | [security](docs/security.md#数据隔离) |
| C9 | 公开样本里仍有可追溯到邮件本身的标识（Message-ID 等） | 不属于身份信息，按隐私边界**接受** | [样本 README](tests/fixtures/README.md) |
| C10 | 汇率接口不可用 | 缓存 + 配置里的兜底汇率，报表里标注来源 | [data-model](docs/data-model.md#汇率) |
| D1 | **新手做不完**：范围太大、步骤太粗 | 第一版只做 M0–M8，每步可验证；其余都标为"以后" | [development](docs/development.md) |
