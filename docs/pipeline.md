# 处理流水线、去重与部署

对应代码：`autobill/pipeline.py`（一封邮件进数据库）、`autobill/store/`、`autobill/service.py`（一次运行的各个步骤）、`autobill/notify/reports.py`（什么时候发报表）、`autobill/cli.py`（命令行，只读参数、调用、打印）。每一步在哪个函数，见 [architecture.md](architecture.md)。

## 状态机
**原则**：fetch → process → report 三段，每段都幂等、都可以重跑。任何一步崩溃，下次运行都能从数据库状态接着处理。

```text
FETCHED ──解析通过──▶ OK / WARN / UNVERIFIED ──▶ 发报表邮件，成功后记下 bills.reported_at
   ├── 确定性错误（TemplateChanged、校验失败、解析时的任何异常）──▶ FAILED（发告警邮件；修好解析器后自动重读）
   ├── 临时错误（IO、网络、数据库锁）──▶ 保持 FETCHED，下次运行自然会重试
   ├── 没有解析器认领 ──▶ UNRECOGNIZED（发告警邮件）
   └── 不是账单，或超出回填范围 ──▶ IGNORED
```

**以后**：临时错误的退避重试和次数上限（RETRY → DEAD）。

- **fetch**：
  1. 原始邮件先写入 `raw/<sha256>.eml`：先写临时文件，再原子重命名；
  2. 在**同一个事务**里写 `emails` 行并推进 `folder_cursors`。
  - 如果在第 2 步之前崩溃，下次会重新拉取，sha256 相同，自然去重。
- **process**：在**一个事务**里完成下面几件事，任何异常都整体回滚：
  - upsert `bills`、`bill_balances`；
  - 整体替换这份账单的流水；
  - 更新 `emails.status`。
  - **解析时的任何错误都记 `FAILED`**，不只是 `TemplateChanged` 和格式错误，PDF 损坏或加密、数值不符合数据模型、解析器自己的 bug 也一样：解析只处理已经存下的原件，同一封邮件每次都会同样失败，属于确定性错误。如果直接抛出去，这次运行会停在它这里，之后每次都停在同一处，后面的邮件全被挡住，也收不到提醒。
  - 之前是 `FAILED` 或 `UNRECOGNIZED` 的邮件再次遇到时会重新处理；其他状态的按 Message-ID 跳过（`SKIPPED`），所以**同一个目录导入两次，数据库不变**。
- **report**：给还没有 `reported_at` 的账单发报表邮件，发送成功后再写入 `reported_at`。这样重复运行不会重发；发送失败的，下次运行会再发一次。
  - 实现在 `notify/reports.py` 的 `send_pending_reports()`：还没报告的账单按账单月分组，**这个月收齐了才发一封**（每张应有的卡已出账或已超时），没收齐就先攒着；不同账单月从早到晚各一封（见 [notify.md](notify.md#账单月邮件)）。**遇到第一次失败就停下**（授权码错或服务器不通时，后面的也都会失败），命令以非零状态退出。
  - 邮件发出后，在同一个事务里写 `reported_at` 和 `cycle_threads`（这个月已发邮件的 Message-ID，用来折叠对话）。

## 三层去重
1. **邮件层**：Message-ID，缺失时用 sha256。
2. **账单层**：`(bank, account_id, statement_date)` 唯一。它兜住"同一期账单经不同邮件到达"的情况：历史批量转发和自动转发重叠、重复转发、银行补发更正。
   - 内容相同：不做任何事。
   - 内容不同：现在没有兜底解析器，所有账单的质量都一样，所以直接用新内容更新账单，并记录日志。
   - **以后**加上兜底解析器时，再引入 `quality`（3 完整明细 > 2 部分成功 > 1 兜底汇总）：**优先级更低的结果永远不能覆盖更高的**。
3. **流水层**：在账单范围内整体替换，**不按内容去重**，因为同一天两笔一模一样的消费是合法的。

## 以后：推送 outbox
等到有多个推送渠道（企业微信），或者需要推送"账单更正"时，再把 `reported_at` 升级成 outbox：
- 读取 PENDING 状态的通知，逐条发送，成功后立即标为 SENT；失败的留到下次重试。
- `(bill_id, channel, kind)` 唯一。`kind` 分为 `bill_new`（首次入库）和 `bill_corrected`（内容被更正），这样更正后的内容也能推送出去。

## 运行层
- **不会同时跑两个**：Docker 里是 `autobill serve` 一个循环；不用 Docker 时是 systemd 的 oneshot 服务加定时器，上一次还没结束时不会再启动一个。所以不需要自己写文件锁。
- **运行中出错**（邮箱登录失败、账单解析失败、不认识的邮件、新卡号）时发提醒邮件，同一个问题只发一次，见 [notify.md](notify.md#提醒邮件)。每次运行的输出在 `docker compose logs` 或 systemd 日志（`journalctl -u autobill`）里。
- **解析器更新后自动重读**：每次运行开头，先把这一版解析器会读得不一样的已入库邮件重新解析一遍（`pipeline.outdated_emails()`），不用手动 `reparse`：
  - 旧版解析器读的账单（`bills.parser_version` 比现在的小）；
  - 解析器有变化时，之前没读成功的邮件（`UNRECOGNIZED`、`FAILED`、`WARN`、`UNVERIFIED`）：修好的解析器也许能读了。每封邮件的原件都在 `raw/` 里，不用重新收信。
  - 读的时候用的是哪一版，记在 `parser_versions` 表（表结构版本 7）。版本没变时这一步什么都不做；之前没读成功的邮件，同样的解析器不会每 30 分钟白读一遍。
  - 和手动 `reparse` 一样：账单原地更新、保留 `reported_at`，**不会重发邮件**；以前没认出来的账单这次第一次读到，当作新账单发。重读后仍然失败的照常发提醒（同一封只提醒一次）。
  - **改了 `config.yaml` 的 `card_aliases` 不算**：解析器没变，要手动 `reparse --all`。
- **程序根本没在运行**（服务器停机、容器没起来）的情况，它没法告警自己。现在接受这个风险：最坏的结果只是某个月没收到报表。

## CLI
| 命令 | 做什么 |
|---|---|
| `run [--no-send] [--rescan]` | 从邮箱拉新邮件（"作为附件"转发的先拆开）、解析，再发该发的报表和提醒。`--rescan` 从头重读文件夹：处理过的跳过，之前失败或不认识的重新处理 |
| `serve [--interval 30]` | 一直运行，每隔几分钟跑一次 `run` 的全部步骤（Docker 默认用它）；某一次出错只记日志，不退出 |
| `import-dir <路径> [--no-send]` | 导入一个目录里的 `.eml` 文件（包括子目录），离线开发和测试都用它；每封邮件输出一行状态，最后给出合计 |
| `check-mailbox` | 登录收信和发信邮箱、列出文件夹里的邮件数，不改动也不发送任何东西 |
| `reparse [--all]` | 用现在的解析器和卡号别名，把已入库的邮件**从 `raw/` 里的原件重新解析**；默认只处理 WARN、UNVERIFIED、FAILED、UNRECOGNIZED 的，`--all` 全部。账单原地更新、保留 `reported_at`，不会重发报表；因为别名换了账户的，旧账户那一行删掉。解析器更新后 `run` 会自动做，手动用于改了卡号别名之后 |
| `resend --cycle 2026-08` | 把某个账单月的邮件按现在的数据重算后再发一封，并进同一个对话；不改账单的已发送状态（见 [notify.md](notify.md#账单月邮件)） |
| `preview-email [--cycle 2026-09] [-o 文件]` | 把某个账单月（默认最新）的邮件写成 HTML 文件，不发信 |
| `year-review --year 2026 [-o 文件] [--send]` | 年度回顾：不带 `-o` 在终端打印纯文本，`-o` 写成 HTML，`--send` 立刻发一封（不算自动发的那封，见 [notify.md](notify.md#年度回顾)） |
| `report --month 2026-08` | 在终端输出某个自然月的消费汇总（按交易日期） |
| `uncategorised [--cycle 2026-09] [--limit 20]` | 列出还没分类的商户，生成可以复制进 `rules.yaml` 的 YAML |
| `classify [--limit 50] [--dry-run] [--retry] [--searches 3]` | 让配置好的 AI 给规则分不出来的商户分类，结果存进数据库、直接生效；`run` 在发报表前也会自动做（见 [notify.md](notify.md#ai-分类)） |
| `statement [--account ABC:0003] [--date …] [--all] [-o 文件夹] [--no-pdf]` | 生成标准账单（HTML 和 PDF，见 [statement.md](statement.md)） |

`import-dir` 和 `run` 不带 `--no-send` 时，处理完会发该发的报表；没配置邮箱或没有授权码时只提示、不发。

**以后**：`rebuild` 清空数据库，从 iCloud 的 `AutoBill` 文件夹全量重新拉取、解析，不受 12 个月回填范围限制。

## 备份
- **iCloud 的 `AutoBill` 文件夹就是原件库**，数据库都可以从它重建。程序对它只读，所以不另外备份原始邮件。
- 前提是那里的银行邮件不被删，也不留在会被自动清理的"垃圾邮件"里（iCloud 规则会把它们移进 `AutoBill`，见 [setup.md](setup.md)）。
- 数据库本身每月随月度邮件备份一份，见 [notify.md](notify.md#每月备份)。
- `fx_rates` 汇率缓存丢了也没关系，重建时会重新获取。

## 部署
**推荐 Docker**，步骤见 [deploy.md](deploy.md)。
- 镜像由 GitHub Actions 构建（amd64 + arm64），每次构建都会在容器里导入样本、生成一封月度邮件，确认镜像里没有任何个人文件，然后发布到 `ghcr.io/yangmingunsw/autobill`（main 分支是 `latest`，版本标签是 `0.2.0` 这样的号）。
- `docker compose up -d` 运行 `autobill serve`：立刻跑一次，之后每 30 分钟一次；某一次出错不会让它停下，下一次照常。只有一个循环，所以不会同时跑两个。
- 配置、数据库、原始邮件都在挂载的 `data/` 文件夹里；密码在 `autobill.env`（600），不进镜像。
- 不用 Docker 时：`deploy/systemd/` 的 oneshot 服务 + 每 30 分钟的定时器。
- 在自己电脑上仍然可以开发和测试（测试用独立的临时数据目录），但服务器跑起来后不要在电脑上再 `run`（`--no-send` 除外），否则报表会发两遍。

## 技术栈
- Python ≥3.12、uv + hatchling、ruff、pytest。
- 依赖：pydantic v2、beautifulsoup4 + lxml、pdfplumber、httpx（汇率）、jinja2（报表邮件和标准账单的模板）、typer、pyyaml、platformdirs。收信用标准库的 `imaplib`，发信用 `smtplib`。
- **不用 PyMuPDF**：它是 AGPL 许可，而本项目要公开。
