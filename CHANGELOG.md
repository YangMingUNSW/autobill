# Changelog

本文件记录 AutoBill 的所有重要变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- M6 分类规则 `categorize.py`（关键词匹配原始描述、不分大小写、第一条命中生效；内置默认规则与 `rules.example.yaml` 相同；`rules.yaml` 或 `AUTOBILL_RULES` 覆盖）；终端月报新增分类占比、返现退款、按卡、近 6 个月趋势、Top 10 商户、未分类商户。

### Changed
- `rules.example.yaml` 补充常见连锁商户。

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
