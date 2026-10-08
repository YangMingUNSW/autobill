# 开发流程

这份文档讲**怎么把 AutoBill 一步步做出来**：环境、节奏、Git 流程、里程碑、测试规范和发布。设计本身（要做什么）见 [project.md](project.md) 和其他 docs；给 AI 助手看的规则见 [CLAUDE.md](../CLAUDE.md)。不熟悉的术语见文末的[词汇表](#词汇表)。

## 1. 开发环境
**前提**：已安装 [uv](https://docs.astral.sh/uv/)（本机已有）和 Git。Python ≥3.12 由 uv 负责，不需要单独安装。

```powershell
uv sync                       # 按 pyproject.toml / uv.lock 安装依赖，第一次和依赖变化后执行
uv run pytest                 # 跑全部测试
uv run pytest --cov           # 同上，再列出覆盖率
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

**数据先行**：做新的报表功能之前，先确认它要用的账单都已经导入、全部解析成功：没有收到"不认识的邮件""账单解析失败"的提醒，或者运行 `reparse`（它会列出所有没读成功的邮件并重读一遍）。不然功能做完、导入历史账单之后才发现有账单没读对，报表就用了错误的数据（农行旧模板 #39、几种入账记错类型 #43 都是这样才发现的）。

## 3. Git 流程
- **`main` 分支始终可以运行**。任何改动都不直接提交到 `main`。
- **每一步开一个分支**，命名为 `类型/简述`，例如：`feat/icbc-parser`、`fix/abc-fx-sign`、`docs/notify-timing`。
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
  2. 等 CI 全绿（§4）。`main` 开了分支保护：`lint, test, identity scan` 和 `gitleaks` 没过就合不了，也不能直接推送到 `main`；不需要别人批准（仓库只有你一个人，GitHub 不让 PR 的作者批准自己的 PR）；
  3. 自己看一遍：至少读 PR 描述里"改变了什么行为"；改了邮件外观或 README 的，先看预览（`preview-email`、`year-review -o`）或截图，确认了再合并。也可以让 Claude 用 `/code-review` 审一遍；
  4. **你自己点合并**：只开了 Squash and merge，一个 PR 在 `main` 上是一个提交（PR 只有一个提交时，用它的英文提交说明）。AI 不负责合并。
- 做到一半想放弃的工作，保留在分支上，不删。合并完的分支也不自动删，什么时候删、删哪些由你决定。
- **什么时候先开 issue**：bug（写清楚现象、哪封账单或哪个月、期望的结果）和要分几个 PR 才能做完的需求，先开 issue，PR 里写 `Fixes #编号` 或 `Refs #编号`；小改动直接开 PR，把"为什么、改变了什么行为"写进 PR 描述。需求和决定不要只留在聊天记录里。

## 4. CI（自动检查）
公开仓库可以免费使用 GitHub Actions。`.github/workflows/ci.yml` 在每次推送和开 PR 时自动执行：

1. `uv run ruff check .` 和 `uv run ruff format --check .`；规则在 `pyproject.toml` 的 `[tool.ruff.lint]`，除了基本的错误和风格，还查笼统的 `except Exception`（BLE）、可以简化的写法（SIM、C4、RET）和测试写法（PT）；
2. `uv run pytest`；合并到 main 时加 `--cov` 顺带统计覆盖率，CI 日志里有一张表（只列没全覆盖的文件），不设门槛。PR 上不统计：统计覆盖率会让测试慢一倍；
3. **身份信息扫描**：
   - 用 gitleaks 扫描；
   - 自定义规则：拦截 18 位证件号、11 位手机号、16 位以上的卡号；
   - `.eml`、`.pdf` 只允许出现在 `tests/fixtures/` 下；
4. **Docker 镜像**：和前三项同时构建并冒烟测试，PR 的结果只等其中最慢的一项。合并到 main 和打版本标签时，等前三项和冒烟测试都通过，才发布到 ghcr.io。

这些都在同一个工作流里，所以每个 PR、每次合并到 main 都只有一个结果，GitHub 只发一条通知（App 推送）。

前三项是 main 的分支保护要求的，任何一项不通过，PR 就不能合并；镜像那一步不在要求里，失败了会在同一条通知里看到。本地的 pre-commit 跑的是同一套检查（前两项和身份扫描），这样在提交之前就能发现问题。

## 5. 里程碑：第一版（M0–M8）
第一版按 M0–M8 分步做完：先用农行把整条链路打通（M0–M3），再加建行、中行（M4–M5，`v0.1.0`），然后是分类、邮件报表、标准账单、iCloud 收信和服务器定时运行（M6–M8，`v0.2.0`）。每一步做了什么见 [CHANGELOG](../CHANGELOG.md) 和 Git 历史。之后的改动不再按里程碑编号：一个 PR 做一件事，按 §2 的节奏来。

**补样本（P1b）**：随时穿插进行，不单独占一个步骤。拿到新样本后按 [security.md 的检查清单](security.md#以后加入新样本时的检查清单) 脱敏，加进 `tests/fixtures/`，补上快照，并更新[样本覆盖矩阵](banks/README.md#样本覆盖矩阵)。

## 6. 测试规范
- **测试不联网**：汇率接口用假数据代替，SMTP 和 IMAP 也用假对象代替。联网的验证只在手动检查时做。
- **不碰真实数据**：用 pytest 的 `tmp_path` 作为 `AUTOBILL_DATA_DIR`。每个测试都用一个全新的空目录。
  - **测试里不要用 `monkeypatch.undo()`**：它会把临时数据目录的设置也一起撤销（真出过一次，碰到了真实的数据库）。要恢复某个被替换的函数，就再 `setattr` 一次原来的函数。
  - 最后一道保险（`tests/conftest.py`）：测试里一旦用到真实的数据目录，或者真的去连 SMTP（465/587）或 IMAP，都会直接报错。
- **要用样本账单，就用 `sample_db` 取一份**（`tests/conftest.py`）：`sample_db(FIXTURES / "abc")` 返回一个数据库连接，里面是导入好的这些样本。同一组样本整个测试过程只导入一次，每个测试拿到的是自己的复制品，改了也不影响别的测试。不要在每个测试里自己导入一遍：一次要一秒多，测试一多，整套就要好几分钟。只有测导入本身的测试才自己导入。
- **快照**：
  - 第一次生成时必须**人工核对**：对账通过是前提，再挑几笔和样本原文逐字比对；
  - 之后快照一有变化，就要在 PR 描述里说明原因。**不能为了让测试通过就直接覆盖快照。**
- **金额一律用 `Decimal`**，不要用浮点数。

## 7. 文档同步规则
- 设计文档和代码包一一对应（见 [project.md 的对照表](project.md#5-文档导航与代码包对照)）。**改了代码的行为，就在同一个分支里改对应的文档。**
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
3. 照着现有几家的格式，写 `docs/banks/<代码>.md` 格式规格。
4. 把它加进 [banks/README.md](banks/README.md) 的注册表和样本覆盖矩阵。
5. 照着现有的解析器写 `parse/<代码>.py` 和快照测试（见 [parsing.md](parsing.md)），在 `parse/registry.py` 登记。

**新增样本**：只做上面的第 2 步，再加上快照测试和覆盖矩阵的更新。

**更新 README 的图片**（`docs/images/`）：月度邮件或年度回顾的样式改了之后，重新生成截图和动画，和样式改动放在同一个 PR 里（PR 模板里有这一条）。
```powershell
uv run --with playwright playwright install chromium   # 第一次用时装一次
uv run --with playwright --with pillow python scripts/demo_screenshots.py   # 截图 + 动画
uv run --with playwright python scripts/readme_art.py                        # 横幅、流程图、logo、分享图
```
- 数据**全部是编的**（脚本里写死的卡、商户、金额），不读任何真实账单和数据库，所以图片可以公开；它不是"脱敏脚本"。
- 编的数据照样走真实代码（`save_bill`、月度报表、年度回顾、模板），截图就是程序实际渲染的样子；全程离线，汇率直接写进临时数据库。编了 2026 全年加 2027 年 1 月的账单，年度回顾才有完整的一年。
- `demo_screenshots.py` 生成 7 张截图（月度邮件首屏的浅色和深色、本期消费、近 6 期、展开的流水、年度回顾首屏、标准账单，每张 300 KB 以内）和 4 段动画（`demo-month-*.webp`：滑到分类、轻点展开；`demo-year-*.webp`：点分类柱子跟着变、再点月份；各有浅色、深色两份，README 用 `<picture>` 跟着 GitHub 的主题换）。
  - 截图时模拟"减弱动态效果"，并等展开的列表停稳再拍。
  - 动画是把页面上的动画全部暂停，每次往前拨 1/30 秒拍一帧，所以动作和邮件里一模一样、不会掉帧；有一个表示手指的圆点，最后淡回第一帧循环播放。每段约 6 秒、1.3–2.4 MB。`--no-animation` 只拍截图。
- `readme_art.py` 画 README 顶部的横幅和"工作原理"流程图（浅色、深色，英文、中文，一共 8 个 SVG），以及 `logo.svg` 和 `social-preview.png`。
  - 颜色取自邮件的 `_email.css`，横幅上的数字取自演示数据。动画是 SVG 里的 CSS（GitHub 把 SVG 当图片显示，不跑脚本），打开页面时播一次（流程图的小点一直流动），系统设置了"减弱动态效果"就不播。
  - `social-preview.png`（1280 × 640）是别人分享仓库链接时显示的大图，GitHub 没有接口可以上传，要在仓库 Settings → General → Social preview 里手动上传。
- Playwright 和 Pillow 只是这两个脚本用，不是项目依赖；已经有 Chrome/Chromium 时可以用 `--browser <路径>` 跳过安装。

## 10. 用 AI（Claude）开发的注意事项
- **一个对话只做一件事**。开头告诉它要做什么、先读哪些文档，比如"加一家银行，先读 CLAUDE.md 和 docs/banks/README.md"。
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
