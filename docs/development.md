# 开发流程

这份文档讲**怎么把 AutoBill 一步步做出来**：环境、节奏、Git 流程、里程碑、测试规范和发布。设计本身（要做什么）见 [project.md](../project.md) 和其他 docs；给 AI 助手看的规则见 [CLAUDE.md](../CLAUDE.md)。不熟悉的术语见文末的[词汇表](#词汇表)。

## 1. 开发环境
**前提**：已安装 [uv](https://docs.astral.sh/uv/)（本机已有）和 Git。Python ≥3.12 由 uv 负责，不需要单独安装。

```powershell
uv sync                       # 按 pyproject.toml / uv.lock 安装依赖，第一次和依赖变化后执行
uv run pytest                 # 跑全部测试
uv run pytest tests/test_abc.py -k unionpay   # 只跑某个文件或某个用例
uv run ruff check .           # 代码检查
uv run ruff format .          # 自动格式化
uv run pre-commit install     # 只需执行一次：以后每次 git commit 前自动检查
uv run autobill --help        # 运行程序本身
```

- **开发时不会碰到你的真实数据**：测试都用 `tests/fixtures/` 里的样本，并在临时目录里运行（见 §6）。真正使用时的数据在 `%LOCALAPPDATA%\autobill\`，和开发互不干扰。

## 2. 每一步的节奏
每个里程碑步骤都按这个循环走：

1. **读文档**：先读这一步对应的设计文档（见 §5 每一步里的链接）。
2. **先写测试**：用 `tests/fixtures/` 的样本写出"期望的结果"。
3. **实现**：写到测试通过为止。
4. **自检**：`uv run ruff check .`、`uv run ruff format .`、`uv run pytest`，全部通过。
5. **同步文档**：代码的行为和文档不一致时，在**同一个分支**里把文档改掉（见 §7）。
6. **记 CHANGELOG**：用一句话写下这一步做了什么（见 §8）。
7. **提交并开 PR**（见 §3）。

## 3. Git 流程
- **`main` 分支始终可以运行**。任何改动都不直接提交到 `main`。
- **每一步开一个分支**，命名为 `类型/里程碑-简述`，例如：`feat/m2-abc-parser`、`fix/abc-fx-sign`、`docs/notify-timing`。
- **提交信息**采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 格式，写成"类型: 做了什么"：

  | 类型 | 用于 | 例子 |
  |---|---|---|
  | `feat` | 新功能 | `feat: 解析农行账务说明汇总块` |
  | `fix` | 修 bug | `fix: 农行购汇行的符号取反` |
  | `test` | 只改测试 | `test: 补充银联卡快照` |
  | `docs` | 只改文档 | `docs: 更新样本覆盖矩阵` |
  | `chore` | 杂项（依赖、配置） | `chore: 升级 pdfplumber` |

  提交要小，一次提交只做一件事，失败时容易回退。
- **合并流程**：
  1. `git push` 推送分支，然后在 GitHub 上开 PR；
  2. 等 CI 全绿（§4）；
  3. 自己看一遍改动，也可以让 Claude 用 `/code-review` 审一遍；
  4. **你自己点合并**。AI 不负责合并。
- 做到一半想放弃的工作，保留在分支上，不删。

## 4. CI（自动检查）
公开仓库可以免费使用 GitHub Actions。在 M0 建立 `.github/workflows/ci.yml`，每次推送和开 PR 时自动执行：

1. `uv run ruff check .` 和 `uv run ruff format --check .`；
2. `uv run pytest`；
3. **身份信息扫描**：
   - 用 gitleaks 扫描；
   - 自定义规则：拦截 18 位证件号、11 位手机号、16 位以上的卡号；
   - `.eml`、`.pdf` 只允许出现在 `tests/fixtures/` 下。

任何一项不通过，PR 就不能合并。本地的 pre-commit 跑的是同一套检查（前两项和身份扫描），这样在提交之前就能发现问题。

## 5. 里程碑：第一版（M0–M8）
第一版的范围见 [project.md](../project.md#2-第一版mvp范围)。**顺序是：先用农行把整条链路打通（M2–M3），再加别的银行。**这样尽早就能看到能用的结果，后面每加一家银行，只是往已经跑通的链路上加一个解析器。

每一步都是一个分支、一个 PR。"怎么验证"写的是你自己能执行的命令，以及应该看到的结果。

### M0 项目骨架
- **交付**：
  - `pyproject.toml`（uv + hatchling，包名 `autobill`，`requires-python >= 3.12`）、`src/autobill/__init__.py`、`tests/test_smoke.py`；
  - ruff、pytest 配置；`.pre-commit-config.yaml`；`.github/workflows/ci.yml`；
  - `config.example.yaml`（内容见 [security.md](security.md#配置示例configexampleyaml入库实际使用的-configyaml-不入库)）、`rules.example.yaml`（见 [notify.md](notify.md#分类规则)）。
- **做完的标准**：CI 在 GitHub 上是绿的。
- **怎么验证**：`uv sync`，然后 `uv run pytest` 全部通过（冒烟测试 1 个 + 身份扫描测试 12 个）；`uv run autobill --help` 能打印出帮助信息；`uv run pre-commit run --all-files` 全部 Passed。

### M1 数据模型和工具函数
- **交付**：`model.py`（按 [data-model.md](data-model.md)）；`parse/util.py`（金额、日期、空白规范化，按 [parsing.md](parsing.md#通用工具)）。
- **做完的标准**：parsing.md "通用工具"里列出的**每一种**金额格式和日期格式，都至少有一个测试用例。
- **怎么验证**：`uv run pytest tests/test_util.py -v`，全部通过。

### M2 农行解析器 + 对账
- **交付**：`parse/abc.py`（按 [banks/abc.md](banks/abc.md)）；`reconcile.py`（分项对账和合成调整）；3 个快照 `tests/snapshots/abc/*.json`。
- **做完的标准**：
  - 3 份农行样本的快照已经**人工核对过**；
  - 分项对账全部通过，银联卡生成了一条 −0.62 的合成调整；
  - 删掉"账务说明"标题的样本会抛出 `TemplateChanged`；
  - 农行的支出在我们这里是正数。
- **怎么验证**：`uv run pytest tests/test_abc.py -v`；打开快照，和 [banks/abc.md §8](banks/abc.md) 的对账表逐项比对。

### M3 第一条完整链路（用农行打通）
- **交付**：
  - `store/`（SQLite 表结构，用唯一键保证导入幂等）；
  - `fetch/source.py` 里的 `DirectorySource`；
  - `fx.py`（Frankfurter + `fx_rates` 缓存 + 兜底汇率）；
  - CLI 命令 `import-dir`、`report --month`（终端输出）。
- **做完的标准**：
  - 同一个目录导入两次，数据库内容不变（幂等）；
  - 月报能显示分币种合计和人民币总计，并注明汇率日期；
  - 测试里不联网。
- **怎么验证**：
  ```powershell
  $env:AUTOBILL_DATA_DIR = "$env:TEMP\autobill-dev"
  uv run autobill import-dir tests/fixtures/abc
  uv run autobill report --month 2026-08
  ```
  能看到 8 月各卡的支出合计；再执行一次 `import-dir`，报表的数字不变。

### M4 建行解析器
- **交付**：`parse/ccb.py`（按 [banks/ccb.md](banks/ccb.md)）+ 快照。
- **做完的标准**：同 M2；"卡号只在交易行"的回退逻辑有测试；消费相关的部分，在文档里仍然标为"推断"。

### M5 中行 PDF 解析器 → 打 `v0.1.0`
- **交付**：`parse/boc.py`（pdfplumber + `dedupe_chars()`，按 [banks/boc.md](banks/boc.md)）+ 快照。
- **做完的标准**：同 M2；零欠款、到期还款日为空的情况处理正确。
- **完成后**：打 `v0.1.0` 标签，表示离线解析三家都完成了。

### M6 分类和报表完善
- **交付**：分类规则引擎；终端月报增加分类占比、按卡分布、近 6 个月趋势、Top 商户、未分类商户列表。
- **做完的标准**：用样本跑出来的报表，每一节都有内容；"财付通"等消费归入"微信 / 支付宝（未细分）"。

### M7 邮件报表
- **交付**（M7c 已替换为账单月进度邮件）：`report/mail_report.py`（Jinja2 + MJML 模板，所有可视化都是 HTML 横条，不用图片）、`notify/mail.py`（SMTP 465）、`autobill preview-email`（本地预览）。每导入一份账单就发一封邮件（见 [notify.md](notify.md#报表什么时候发)）。
- **先看预览**：真实发信之前，`uv run autobill preview-email -o preview.html` 生成报表，用浏览器打开、按 F12 切到手机尺寸检查排版。
- **做完的标准**：用 `import-dir` 导入一份样本后，主邮箱收到报表，**用手机打开**排版正常，图片能显示。
- **测试**：SMTP 用假对象代替，测试不真正发信；真实发信只在手动验证时做一次。

### M7b 标准账单（2026-09-19 插入）
- **交付**：`report/statement.py` 和模板 `statement.html.j2`、`report/pdf.py`（用本机的 Edge/Chrome 打印 PDF）、命令 `autobill statement`。设计见 [statement.md](statement.md)。
- **做完的标准**：
  - 8 份样本账单都能生成 HTML，本机有浏览器时也生成 PDF；
  - 每一笔流水都在；账户摘要等于对账恒等式；没有脚本和外部资源；
  - 作者在 iPhone 的"文件"App 里打开看过并认可。
- **怎么验证**：
  ```powershell
  $env:AUTOBILL_DATA_DIR = "$env:TEMP\autobill-dev"
  uv run autobill import-dir tests/fixtures --no-send
  uv run autobill statement --all -o "$env:USERPROFILE\iCloudDrive\AutoBill-预览"
  ```
  然后在 iPhone 的"文件"App → iCloud 云盘 → AutoBill-预览 里打开 HTML 和 PDF。

### M7c 账单月进度邮件（2026-09-19 插入）
- **交付**：`report/cycle.py` + 模板 `cycle_report.html.j2`（只为 iPhone 苹果邮件排版）、`report/style.py`（共用配色和横条）、卡包配置 `cards.portfolio`、`cycle_threads` 表、`preview-email --cycle`。替换 M7 的"每份账单一封"。设计见 [notify.md](notify.md#账单月进度邮件)。
- **做完的标准**：
  - 按样本导入：每个账单月一封，同一次运行的 3 份农行账单合成一封，各附一份标准账单 PDF；
  - 待出账 / 可能无账单按通常账单日 + 7 天判断；超时后补发"已齐"，已齐的月份不再发；
  - 第二封起带 `In-Reply-To` / `References`；
  - M8a 设好 iCloud 后，真实发一封到 iCloud 邮箱，在 iPhone 上看折叠、深色模式和附件。
- **怎么验证**：
  ```powershell
  uv run autobill import-dir tests/fixtures --no-send
  uv run autobill preview-email -o "$env:TEMP\cycle.html"
  ```
  用 WebKit 内核（Playwright WebKit，和 iOS 同内核）在 390 像素宽下截浅色、深色图检查。

### M7d 分类覆盖率和 AI 接口（2026-09-19 插入）
- **交付**：
  - 分类规则支持整词匹配 `word:`，原始描述和商户名一起匹配；
  - 默认规则加上行业通用词和"烟酒"分类；
  - `autobill uncategorised`：列出未分类的商户，生成可以复制进 `rules.yaml` 的 YAML；
  - AI 接口 `autobill/suggest.py` + `config.yaml` 的 `ai`，`--suggest` 调用；这一步不接任何 AI。
  - 设计见 [notify.md](notify.md#分类规则)。
- **做完的标准**：样本里未分类的消费从 86 笔降到 40 笔以内；每条新词都核对过没有误分；AI 只收到商户名和分类名，建议不会自动写进 `rules.yaml`。
- **怎么验证**：
  ```powershell
  uv run autobill import-dir tests/fixtures --no-send
  uv run autobill uncategorised --limit 10
  ```

### M8a iCloud 收信（2026-09-19 拆分）
- **交付**：
  - `fetch/imap.py`：只读的 `Mailbox` 和 `ImapSource`，文件夹游标（`folder_cursors` 表）、只处理发给别名的邮件、跳过自己的报表；
  - `fetch/mime.py`：拆开"作为附件"转发的邮件；
  - 发信支持 587 端口 + STARTTLS（iCloud）；发信密码没设时用收信密码；
  - 命令 `check-mailbox`、`run [--no-send]`；
  - setup.md 改成 iCloud 版本的操作步骤。设计见 [fetcher.md](fetcher.md#imap)。
- **做完的标准**：
  - 测试（假 IMAP 服务器）：只读、处理完才前进、UIDVALIDITY 变了重读、只处理发给别名的、拆附件、STARTTLS；
  - 作者按 setup.md 设好后，在家里电脑上 `check-mailbox` 全部正常；转发一封真实账单，`run` 解析成功，iPhone 收到进度邮件；再运行一次不重复发送。
- **怎么验证**：见 [setup.md 第 5 步](setup.md#5-运行m8a-手动m8b-放到服务器上定时运行)。

### M8b 服务器定时运行和提醒 → 打 `v0.2.0`（第一版可用）
- **交付**：
  - `notify/alerts.py`：不认识的邮件、解析失败、新卡号、邮箱登录失败时发提醒邮件，同一个问题只发一次（`alerts` 表，表结构版本 4）；
  - `deploy/systemd/`：oneshot 服务 + 每 30 分钟的定时器（不会同时跑两个，所以不需要文件锁）；
  - [deploy.md](deploy.md)：服务器上的安装、密码文件、日常命令；
  - 服务器上装 `fail2ban`。
- **做完的标准**：服务器上 `check-mailbox` 全部正常；定时器按时运行；一封真实账单被银行发到别名后，30 分钟内收到进度邮件；重复运行不会重复发送；故意用错密码时收到"登录邮箱失败"的提醒（发信密码单独设置时）。
- **完成后**：打 `v0.2.0` 标签，**第一版可用**。

**补样本（P1b）**：随时穿插进行，不单独占一个步骤。拿到新样本后按 [security.md 的检查清单](security.md#以后加入新样本时的检查清单) 脱敏，加进 `tests/fixtures/`，补上快照，并更新[样本覆盖矩阵](banks/README.md#样本覆盖矩阵)。

## 6. 测试规范
- **测试不联网**：汇率接口用假数据代替，SMTP 和 IMAP 也用假对象代替。联网的验证只在手动检查时做。
- **不碰真实数据**：用 pytest 的 `tmp_path` 作为 `AUTOBILL_DATA_DIR`。每个测试都用一个全新的空目录。
  - **测试里不要用 `monkeypatch.undo()`**：它会把临时数据目录的设置也一起撤销（2026-09-19 真出过一次，碰到了作者的真实数据库）。要恢复某个被替换的函数，就再 `setattr` 一次原来的函数。
  - 最后一道保险（`tests/conftest.py`）：测试里一旦用到真实的数据目录，或者真的去连 SMTP（465/587）或 IMAP，都会直接报错。
- **快照**：
  - 第一次生成时必须**人工核对**：对账通过是前提，再挑几笔和样本原文逐字比对；
  - 之后快照一有变化，就要在 PR 描述里说明原因。**不能为了让测试通过就直接覆盖快照。**
- **金额一律用 `Decimal`**，不要用浮点数。

## 7. 文档同步规则
- 设计文档和代码包一一对应（见 [project.md 的对照表](../project.md#5-文档导航与代码包对照)）。**改了代码的行为，就在同一个分支里改对应的文档。**
- 新的决策写进 project.md 的决策表；版本变化写进 [research.md 的版本历史](research.md#版本历史)。
- 某个功能从"以后"挪进"第一版"，或者反过来，都要同时改 project.md 的范围一节。

## 8. 版本与发布
- 版本号采用 [SemVer](https://semver.org/lang/zh-CN/) 的 0.x 阶段：`v0.1.0`（M5，离线解析完成）→ `v0.2.0`（M8，第一版可用）→ 之后每加一个功能就升次版本号，修 bug 就升修订号。
- **CHANGELOG.md** 采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 格式，git init 时建立，从 M0 开始记录：每个 PR 在 `[Unreleased]` 下加一行，打标签时把这些行移到新版本号下面。
- 打标签：在 `main` 上执行 `git tag v0.1.0 && git push --tags`。

## 9. 常用流程清单
**新增一家银行**
1. 从邮箱里导出这家银行至少 1 期账单，最好 3 期。
2. 按 [security.md 的检查清单](security.md#以后加入新样本时的检查清单) 脱敏，放进 `tests/fixtures/<代码>/`。
3. 照着现有三家的格式，写 `docs/banks/<代码>.md` 格式规格。
4. 把它加进 [banks/README.md](banks/README.md) 的注册表和样本覆盖矩阵。
5. 写 `parse/<代码>.py` 和快照测试（流程同 M2）。

**新增样本**：只做上面的第 2 步，再加上快照测试和覆盖矩阵的更新。

## 10. 用 AI（Claude）开发的注意事项
- **一个对话只做一个里程碑步骤**。开头告诉它"做 M2，先读 CLAUDE.md 和 docs/banks/abc.md"。
- **先让它给出计划，看过之后再让它动手。**
- **自己读 diff 和测试**：看不懂的地方就让它解释。测试就是最好的说明书，它写明了"输入什么、应该得到什么"。
- **命令自己跑一遍**，不要只看 AI 说"通过了"。
- **不要把原始账单贴进公开的 issue、PR 或网页**。需要举例时，用 `tests/fixtures/` 里的样本。
- 项目规则变化了（比如新增了一条红线），就更新 [CLAUDE.md](../CLAUDE.md)。

## 词汇表
| 术语 | 解释 |
|---|---|
| 分支（branch） | 代码的一条平行副本。在分支上改，改坏了也不影响 `main`，满意了再合并回去 |
| PR（Pull Request） | 在 GitHub 上申请"把分支合并进 main"。它会显示改了哪些地方，CI 结果也显示在这里 |
| CI（持续集成） | 每次推送代码时，GitHub 自动运行检查和测试，结果是绿（通过）或红（失败） |
| fixture（测试样本） | 测试用的固定输入。这里就是 `tests/fixtures/` 里脱敏后的账单 |
| 快照测试 | 把解析结果存成 JSON 文件，以后每次都和它比较。结果一变，测试就会失败，提醒你检查 |
| 幂等 | 同一件事做一次和做多次，结果一样。比如同一封账单导入两次，数据库里不会多出一份 |
| Conventional Commits | 提交信息的写法约定：`类型: 描述`，例如 `feat: 解析建行汇总表` |
| SemVer | 版本号 `主.次.修订`。0.x 表示还在早期，接口可能会变 |
