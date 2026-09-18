# 处理流水线、去重与部署

对应代码：`autobill/pipeline.py`、`autobill/store/`、`autobill/cli.py`。

## 状态机
**原则**：fetch → process → report 三段，每段都幂等、都可以重跑。任何一步崩溃，下次运行都能从数据库状态接着处理。

**第一版**：

```text
FETCHED ──解析通过──▶ OK / WARN / UNVERIFIED ──▶ 发报表邮件，成功后记下 bills.reported_at
   ├── 确定性错误（TemplateChanged、校验失败）──▶ FAILED（发告警邮件；修好解析器后 reparse）
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
- **report**：给还没有 `reported_at` 的账单发报表邮件，发送成功后再写入 `reported_at`。这样重复运行不会重发；发送失败的，下次运行会再发一次。

## 三层去重
1. **邮件层**：Message-ID，缺失时用 sha256。
2. **账单层**：`(bank, account_id, statement_date)` 唯一。它兜住"同一期账单经不同邮件到达"的情况：历史批量转发和自动转发重叠、重复转发、银行补发更正。
   - 内容相同：不做任何事。
   - 内容不同：**第一版**没有兜底解析器，所有账单的质量都一样，所以直接用新内容更新账单，并记录日志。
   - **以后**加上兜底解析器时，再引入 `quality`（3 完整明细 > 2 部分成功 > 1 兜底汇总）：**优先级更低的结果永远不能覆盖更高的**。
3. **流水层**：在账单范围内整体替换，**不按内容去重**，因为同一天两笔一模一样的消费是合法的。

## 以后：推送 outbox
等到有多个推送渠道（企业微信），或者需要推送"账单更正"时，再把 `reported_at` 升级成 outbox：
- 读取 PENDING 状态的通知，逐条发送，成功后立即标为 SENT；失败的留到下次重试。
- `(bill_id, channel, kind)` 唯一。`kind` 分为 `bill_new`（首次入库）和 `bill_corrected`（内容被更正），这样更正后的内容也能推送出去。

## 运行层
- 单实例文件锁，防止上一次还没跑完、下一次定时任务又启动。
- 每次运行写一条 `runs` 记录。**运行中出错**（授权码失效、IMAP 连不上等）时发告警邮件。
- **程序根本没在运行**（关机、休眠、断网）的情况，本机没法告警自己，只能靠**以后**的 Oracle 看门狗（见 [notify.md](notify.md#以后企业微信中转与看门狗)）。第一版接受这个风险：最坏的结果只是某个月没收到报表。

## CLI
`autobill run | fetch | process | report --month YYYY-MM | import-dir <路径> | reparse [--bank X --since YYYY-MM] | rebuild | status`

- `import-dir`：直接导入一个目录里的 .eml 文件，离线开发和测试都用它（对应 `DirectorySource`）。
- `report --month`：在终端输出某个月的汇总。M3–M6 先用它看结果，M7 开始发邮件。
- `rebuild`：清空数据库，从中心邮箱全量重新拉取、解析，**不受 12 个月回填范围限制**。

## 备份
- **中心邮箱就是原件库**，本机数据库都可以从它重建（`rebuild`）。所以本机不另外做备份。
- 前提是中心邮箱里的银行邮件不能被删，也不能被垃圾箱自动清理（见 [setup.md](setup.md)）。
- `fx_rates` 汇率缓存丢了也没关系，重建时会重新获取。

## 部署
**第一版：家里这台 Windows 电脑**
- 用 `uv` 安装；Windows 计划任务每 15 分钟执行一次 `autobill run`。
- 计划任务的两个设置：
  - **"仅在用户登录时运行"**，否则读不到 Windows 凭据管理器里的授权码；
  - **"错过计划时间后尽快运行"**，否则休眠期间错过的任务不会补跑。
- 电脑关机就不跑，开机后自动补上。账单离还款日一般还有二十多天，晚几个小时没有关系。
- 开发和运行在同一台电脑上，测试必须用独立的数据目录（`AUTOBILL_DATA_DIR`），不能碰真实数据库。

**以后：Oracle 只跑中转服务和看门狗**
- 数据和授权码**始终留在家里**，不整体搬到云上。
- Oracle 上只部署一个很小的中转服务，用 uv + systemd 运行，不用 Docker（机器只有 1 GB 内存）。
- 把临时公网 IP 换成固定 IP，家里电脑加一个 WireGuard peer。

## 技术栈
- Python ≥3.12（本机装有 3.12 和 3.14）、uv + hatchling、ruff、pytest。
- 依赖：pydantic v2、pydantic-settings、imapclient、beautifulsoup4 + lxml、pdfplumber、dkimpy、httpx（汇率）、matplotlib（报表图表）、jinja2、premailer、typer、platformdirs、keyring。
- **不用 PyMuPDF**：它是 AGPL 许可，而本项目要公开。
