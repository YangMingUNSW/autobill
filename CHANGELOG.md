# Changelog

本文件记录 AutoBill 的所有重要变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- M0 项目骨架：`pyproject.toml`（uv + hatchling）、`autobill` 命令行空壳（`--help`、`--version`）、冒烟测试。
- 身份扫描 `scripts/check_identity.py`，接入 pre-commit 和 GitHub Actions CI（ruff、pytest、gitleaks）。
- 配置示例 `config.example.yaml`、分类规则示例 `rules.example.yaml`。
- M1 数据模型 `model.py`（金额只接受 `Decimal`，`make_txn_id()`）和解析工具 `parse/util.py`（空白、金额、币种、日期、PDF 识别）。
- M2 农行解析器 `parse/abc.py`、三步分项对账 `reconcile.py`、邮件读取 `fetch/message.py`（`RawMessage`）、解析器接口 `parse/base.py`；3 份农行样本的快照。
