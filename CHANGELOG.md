# Changelog

本文件记录 AutoBill 的所有重要变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- M8b 提醒邮件：不认识的邮件、账单解析失败、发现新卡号、邮箱登录失败时发一封提醒到 iCloud，同一个问题只提醒一次（`alerts` 表，表结构升级到 4）；`run --no-send` 时提醒留到下次一起发。
- M8b Docker 部署：`Dockerfile`、`compose.yaml`、`autobill.env.example`；新命令 `autobill serve`（每隔几分钟运行一次，单次出错不退出）；GitHub Actions 构建并冒烟测试镜像，main 和版本标签发布 amd64 + arm64 镜像到 ghcr.io。备选：`deploy/systemd/` 的定时器。`docs/deploy.md` 操作步骤。
- Linux 上打印 PDF 时 Chrome 加 `--disable-dev-shm-usage`。
- 进度邮件默认不再附标准账单 PDF（2026-09-19 作者决定：原始账单直接在邮箱里看，进度邮件不再附 PDF）：新配置 `statement.email_pdf`（默认 false）；不附时邮件里没有附件那一行。Docker 镜像因此不带浏览器和中文字体，两阶段构建，从 1.72 GB 降到约 330 MB。
- 卡号别名 `cards.card_aliases`：换卡或一户两卡时，账单记在同一个账户下；全部卡号都合并到一个账户时，去掉建行"多个卡号"的告警。

### Fixed
- 身份扫描加本地私密词表（`private-terms.txt`，不入库）：真实卡号后四位、邮箱地址这类没有固定格式的内容也能在提交前拦下。
- 农行：认识 `取现/转出`（境外取现 → 取现）和 `利息`（→ 利息）两个分组。以前被记成"调整"，借方对不上，对账告警（作者 2026-06 VISA 账单实测发现）。

### Added
- M8a iCloud 收信：`autobill check-mailbox`（登录检查，不改动、不发送）和 `autobill run`（拉取、解析、发进度邮件）；IMAP 只读（EXAMINE + BODY.PEEK[]），按文件夹记 UIDVALIDITY + last_uid，处理完才前进；只处理发给别名的邮件、跳过自己的报表；拆开"作为附件"转发的邮件（包括 QQ 邮箱把原邮件当成 `.eml` 文件附件的做法；手动导入历史账单用；新账单由银行直接发到 iCloud 别名）；文件夹名不区分大小写，邮箱里暂时没有的文件夹跳过；之前失败或不认识的邮件再遇到时重新处理，`run --rescan` 从头重读；发信支持 587 + STARTTLS（iCloud），发信密码没设时用收信密码；数据库表结构升级到 3（`folder_cursors`）。
- M7d 分类：整词关键词 `word:`；原始描述和商户名一起匹配；默认规则加上行业通用词和"烟酒"分类（样本里未分类的消费从 86 笔降到 37 笔）；`autobill uncategorised` 列出未分类的商户并生成 rules.yaml 片段；预留 AI 分类建议接口（`autobill/suggest.py`、`config.yaml` 的 `ai`，`--suggest`），只发商户名、建议不自动生效。
- M7c 账单月进度邮件：按出账月份给每个账单月发邮件（已出账 N/M、待出账、可能无账单；新账单的每日消费图和分类；附标准账单 PDF；已齐时加还款日一览和全部卡的分类），同一个月的邮件用 In-Reply-To 折叠成一个对话；只为 iPhone 苹果邮件排版，iOS 原生 App 质感（大标题、分组列表、进度圆环、钱包式卡片、日历式还款日、屏幕使用时间式柱状图、Copilot Money 式 emoji 分类和分段条；深色模式；关闭数字自动识别）；逐笔流水和商户排行用 CSS 复选框技巧折叠，默认收起。卡包配置 `cards.portfolio`；`preview-email --cycle`；数据库表结构升级到 2（`cycle_threads`，自动迁移）。
- M7b 标准账单：`autobill statement` 为每份账单生成统一模板的 HTML 和 PDF（账户摘要即对账恒等式、每日消费柱状图、分类、按天分组的全部逐笔流水；PDF 用本机的 Edge/Chrome 打印）。
- M7 邮件报表：每导入一份账单发一封（本期摘要 + 涉及月份的最新汇总 + 还缺哪些卡 + 各卡还款日），发送成功才记 `reported_at`、不重发；MJML 排版，所有可视化是 HTML 横条（不用图片）；`import-dir --no-send`、`preview-email` 本地预览；SMTP 授权码只从环境变量读取。
- M6 分类规则 `categorize.py`（关键词匹配原始描述、不分大小写、第一条命中生效；内置默认规则与 `rules.example.yaml` 相同；`rules.yaml` 或 `AUTOBILL_RULES` 覆盖）；终端月报新增分类占比、返现退款、按卡、近 6 个月趋势、Top 10 商户、未分类商户。

### Changed
- `rules.example.yaml` 补充常见连锁商户。
- M7c：报表邮件从"每份账单一封"改为按账单月的进度邮件；去掉 MJML 依赖，删除旧模板 `bill_report.mjml.j2`；`preview-email` 的 `--account` 改为 `--cycle`。

## [0.1.0] - 2026-09-19
离线解析三家银行完成（第一版里程碑 M0–M5）。

### Changed
- `pdfplumber` 从开发依赖改为正式依赖（中行解析器要用）。
- 中行 2025-06 样本：两个第三方个人收款人姓名替换为假名。
- 建行解析器：卡号栏 `实体/Apple Pay 设备号` 取实体卡；返现识别为 `REBATE`；币种列表扩大到约 50 个（含 CHF）。
- 身份扫描：卡号只认 16–19 位连写或 4 位一组，不再把相邻日期误报成卡号。
- README 改为中文为主（`README.md`），英文版移到 `README.en.md`；GitHub 仓库简介同步改成中文在前。

### Added
- M5 中行 PDF 解析器 `parse/boc.py`：合并账单按卡拆成多份账单、跨页明细、描述折行拼接、Apple Pay 设备号归入实体卡、总览和卡表交叉校验；加入注册表；两份中行样本快照。
- 补样本：建行 2026-06（69 笔，Apple Pay、欧洲外币消费、返现）、中行 2025-06（两卡合并、12 页、有明细）；建行快照。
- M0 项目骨架：`pyproject.toml`（uv + hatchling）、`autobill` 命令行空壳（`--help`、`--version`）、冒烟测试。
- 身份扫描 `scripts/check_identity.py`，接入 pre-commit 和 GitHub Actions CI（ruff、pytest、gitleaks）。
- 配置示例 `config.example.yaml`、分类规则示例 `rules.example.yaml`。
- M1 数据模型 `model.py`（金额只接受 `Decimal`，`make_txn_id()`）和解析工具 `parse/util.py`（空白、金额、币种、日期、PDF 识别）。
- M2 农行解析器 `parse/abc.py`、三步分项对账 `reconcile.py`、邮件读取 `fetch/message.py`（`RawMessage`）、解析器接口 `parse/base.py`；3 份农行样本的快照。
- M3 第一条完整链路：SQLite 存储 `store/db.py`、`DirectorySource`、银行注册表、`pipeline.py`、汇率 `fx.py`（Frankfurter + 缓存 + 配置兜底）、配置 `config.py`；命令 `autobill import-dir` 和 `autobill report --month`。
- M4 建行解析器 `parse/ccb.py`（锚点定位、全 0 外币行跳过、取不到卡号时记为 `CCB:unknown`），加入注册表；建行样本快照。
