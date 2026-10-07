# 代码结构

一封账单从邮箱到月度邮件，经过哪些文件、入口是哪个函数。邮箱、转发、只读这些系统层面的设计见 [project.md 的架构](project.md#4-架构标--的是以后)，每一步的细节见对应的设计文档。下面的路径都相对于 `src/autobill/`。

## 一次运行
`autobill run`，或者 Docker 里的 `autobill serve`（每 30 分钟一次），做的都是 `service.py` 的 `run_once()`：

```text
service.run_once()
 ├─ reparse_outdated()       解析器更新过：用 raw/ 里的原件重读受影响的邮件
 ├─ ImapSource.iter_new()    fetch/imap.py：只读 AutoBill 文件夹里的新邮件
 ├─ import_mails()           拆开"作为附件"转发的邮件（fetch/mime.py），每封交给 pipeline.process()
 ├─ alerts.from_outcomes()   不认识的邮件、解析失败、新卡：记下要提醒的事
 ├─ auto_classify()          categories/ai.py：AI 给规则分不出来的新商户分类
 ├─ send_reports()           notify/reports.py：收齐的账单月发一封；1 月那封之后发年度回顾
 └─ send_alerts()            notify/alerts.py：记下的提醒合成一封发出
```

`import-dir` 只用到其中两步：从目录读 `.eml`（`fetch/source.py`）交给 `import_mails()`，不带 `--no-send` 时再 `send_reports()`。

## 一封邮件进数据库
`pipeline.py` 的 `process()`：

```text
pipeline.process()
 ├─ RawMessage.from_bytes()   fetch/message.py：正文和附件，PDF 按内容开头的 %PDF- 认
 ├─ Message-ID 处理过         → SKIPPED（之前失败或不认识的会重新处理）
 ├─ save_raw()                原件存进 raw/<sha256>.eml
 ├─ find_parser()             parse/registry.py：认出银行，交给 parse/abc.py、ccb.py、boc.py、icbc.py
 │   └─ parser.parse()        得到 model.Bill；解析器最后用 reconcile.py 的 reconcile() 对账
 ├─ apply_aliases()           config.yaml 里的卡号别名
 └─ 一个事务                  emails 一行 + store/db.py 的 save_bill()
```

解析时的任何异常都记成 `FAILED`，不往外抛：同一封邮件每次都会同样失败，抛出去会挡住后面所有邮件（见 [pipeline.md](pipeline.md#状态机)）。

## 月度邮件
`notify/reports.py` 的 `send_pending_reports()`：

```text
send_pending_reports()
 ├─ cycle_complete()          report/cycle.py：这个月收齐没有（只查哪些账户出过账，不渲染）
 ├─ build_cycle_email()       report/cycle_mail.py：HTML、纯文本、对话邮件头
 │   └─ build_cycle_report()  report/cycle.py：这一期的全部数字（CycleReport）
 │       ├─ build_view()      report/statement.py：一份账单折成人民币、分好类
 │       ├─ drill.py          展开后的每一笔
 │       └─ charts.py         圆盘图、近 6 期柱状图（SVG）
 ├─ _attach_backup()          backup.py：附上压缩的数据库
 └─ Mailer.send()             notify/mail.py；发成功才记 reported_at 和 cycle_threads
```

年度回顾是同样的路数：`report/year.py` 算数字，`report/year_explorer.py` 做按月份和分类的筛选，`report/year_mail.py` 做成邮件。模板都在 `report/templates/`，两种邮件共用 `_email.css`。

## 模块之间的关系
- `model.py`（`Bill`、`Transaction` 这些数据结构）谁都可以用，它自己不依赖别的模块。
- 解析器（`parse/`）只把一封邮件变成 `Bill`，不碰数据库、不发信，所以测解析器只需要样本文件。
- 报表（`report/`）读数据库、算数字、做成邮件，但不负责发；什么时候发、发给谁归 `notify/`。
- 口径（哪些交易算消费、金额怎么取整到分、日期归哪个月）只在 `ledger.py` 一处，所有报表共用。汇率在 `fx.py`，分类规则在 `categories/rules.py`。
- `service.py` 把这些串成一次运行；`cli.py` 只读参数、调用、打印。

## 想改什么，去哪里
| 想改的 | 去哪里 |
|---|---|
| 加一家银行 | 按 [development.md 的清单](development.md#9-常用流程清单)：样本、`docs/banks/` 的规格、`parse/<代码>.py`、在 `parse/registry.py` 登记、测试和快照 |
| 哪些交易算消费、算哪个月 | `ledger.py` |
| 默认分类规则 | `categories/default_rules.yaml` 和仓库根目录的 `rules.example.yaml`，两份必须一样（有测试检查） |
| AI 分类 | `categories/ai.py`（问哪些商户、问几次）、`categories/deepseek.py`（提示词和接口） |
| 月度邮件的样子 | `report/templates/cycle_report.html.j2` 和 `_email.css`；数字在 `report/cycle.py`，纯文本在 `report/cycle_mail.py` |
| 什么时候发邮件 | `notify/reports.py`；"收齐"的判断在 `report/cycle.py` 的 `cycle_complete()` |
| 提醒邮件 | `notify/alerts.py` |
| 数据库的表 | `store/db.py`：在 `MIGRATIONS` 里加下一步，`SCHEMA_VERSION` 加一。`SCHEMA` 是第一版的表，新建的库也是它再加上全部迁移，所以不要改它 |
| 命令行 | `cli.py`；命令背后的流程在 `service.py` |
