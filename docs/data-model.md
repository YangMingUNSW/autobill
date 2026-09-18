# 数据模型、符号约定与对账

对应代码：`autobill/model.py`、`autobill/reconcile.py`，存储层在 `autobill/store/`。

## 符号约定
`amount` 按**持卡人视角**记账：
- **正数 = 欠款增加**：消费、手续费、利息、分期本金、购汇转出；
- **负数 = 欠款减少**：还款、退款、返现、购汇转入。

各银行的原始符号要在解析器里统一转换成这个约定：

| 银行 | 明细里的原始符号 | 转换方法 | 汇总块 |
|---|---|---|---|
| 农行 | "入账金额"**支出为负** | `amount = −原值` | 账务说明全为正数，表示欠款；账户信息区是"欠款为负"，只用来交叉校验 |
| 建行 | 还款为负（消费为正是推断） | 不转换 | 正数表示欠款 |
| 中行 | 没有负号，用"存款/欠款"标签表示方向 | 按标签定方向 | 同左 |

## 模型

```python
class TxnType(StrEnum):
    PURCHASE = "purchase"        # 消费
    REFUND = "refund"            # 退款
    REPAYMENT = "repayment"      # 还款（真实资金流入）
    FEE = "fee"                  # 手续费、年费
    INTEREST = "interest"        # 利息（含分期利息，配合 installment 字段）
    CASH = "cash"                # 取现
    INSTALLMENT = "installment"  # 分期本金（本期应还部分）
    REBATE = "rebate"            # 返现（农行计入"本期还款、退货金额"）
    FX_TRANSFER = "fx_transfer"  # 自动购汇的内部换汇：人民币账户转出、外币账户转入，不是消费也不是还款
    ADJUSTMENT = "adjustment"    # 调整；未列明细的调整会生成一条 synthetic=True 的合成流水

class Transaction(BaseModel):
    line_no: int                  # 在账单内的顺序，合成流水排在最后
    txn_id: str                   # 稳定 ID：hash(银行, 账户, 交易日, 金额, 描述, line_no)
    trans_date: date
    post_date: date | None
    txn_type: TxnType
    amount: Decimal               # 结算金额，按上面的符号约定
    currency: str                 # 结算币种
    orig_amount: Decimal | None   # 原币金额，只在和结算币种不同时填（如澳元消费按美元入账）
    orig_currency: str | None
    fx_rate: Decimal | None       # 购汇汇率，如"汇率:6.7648500"
    description_raw: str          # 账单原文，已做空白规范化
    group_raw: str | None         # 银行自己的分组名，如农行的"消费/还款/分期/其他"
    merchant: str | None          # 清洗后的商户名
    merchant_location: str | None # 境外消费的地点字段
    card_last4: str | None        # 购汇、分期本金行可能为空
    installment: str | None       # "3/36"
    category: str | None          # 规则分类结果
    synthetic: bool = False       # 对账时生成的合成流水

class BillBalance(BaseModel):     # 每个币种一份
    currency: str
    previous_balance: Decimal     # 上期欠款（≥0）
    previous_deposit: Decimal = 0 # 上期溢缴款（≥0）
    new_charges: Decimal          # 本期新增（借方）
    interest_fees: Decimal = 0    # 只在银行把利息和费用单独列在汇总块时使用
    payments_credits: Decimal     # 本期还款、退货（贷方，≥0）
    adjustments: Decimal = 0      # 正数 = 增加欠款；农行"本期调整金额"要取反
    amount_due: Decimal           # 本期欠款（≥0）
    deposit: Decimal = 0          # 本期溢缴款（≥0）
    min_payment: Decimal | None

class Bill(BaseModel):            # 一封邮件可以产出多份 Bill（中行合并账单），见 parsing.md 的接口
    bank: str                     # ABC / CCB / BOC
    account_id: str               # "ABC:0002"；建行取不到卡号时为 "CCB:unknown"
    cards: list[str]
    statement_date: date          # 农行没有单独写出，取账单周期结束日
    period_start: date | None
    period_end: date | None
    due_date: date | None         # 不需要还款时可能为空（中行实测）；只用于展示，不做提醒
    email_date: date              # 账单邮件的 Date 头，决定用哪一天的汇率折算人民币
    balances: list[BillBalance]
    transactions: list[Transaction]
    status: Literal["OK", "WARN", "UNVERIFIED"]
    warnings: list[str] = []
    quality: int = 3              # 以后：3 = 完整明细，2 = 部分成功，1 = 兜底汇总（第一版恒为 3）
    reported_at: datetime | None  # 报表邮件发送成功的时间；为空表示还没发（第一版用它代替 outbox）
    source_message_id: str
    source_sha256: str
    parser_name: str
    parser_version: int
```

## 对账

**只看"Σ流水 = 净变化"是不够的。** 农行银联卡有一笔"本期调整金额 0.62"，明细里却没有对应的行：Σ流水 = 9.31，净变化 = 8.69，差的正好是这 0.62。所以对账分成三步，每个币种单独做，全部用 `Decimal` 精确比较。

1. **汇总恒等式**（银行自己印在账单上的算式）：
   `(amount_due − deposit) = (previous_balance − previous_deposit) + new_charges + interest_fees − payments_credits + adjustments`
2. **分项核对**（排除合成流水）：
   - `Σ(amount > 0) = new_charges + interest_fees`（仅当利息在明细中逐笔列出时才加 interest_fees）
   - `Σ|amount < 0| = payments_credits`
3. **未列明细的调整**：如果 `adjustments` ≠ Σ(明细里的 ADJUSTMENT)，就补一条合成流水：`ADJUSTMENT, amount = 差额, synthetic = True, description = "账单调整（未列明细）"`。补完之后，`Σ全部流水 = 净变化` 必然成立，将来导出 Beancount 时 `balance` 断言才不会差这笔钱。

**判定**：

| 情况 | 状态 |
|---|---|
| 三步全部通过 | `OK` |
| 汇总块缺字段，无法做第 1 步 | `UNVERIFIED`：不告警，报表里注明"未对账" |
| 任何一步对不上 | `WARN`：告警，并写明是哪一步、差多少 |

**实测**：6 个币种区块全部通过，明细见 [banks/abc.md](banks/abc.md) 第 8 节和 [banks/ccb.md](banks/ccb.md) 第 4 节。旧设计里的两个公式都被样本否定了：

| 公式 | 反例 |
|---|---|
| v1："Σ流水 = 本期应还" | 建行：Σ = −15,450.83，本期应还 = 0 |
| v2："Σ流水 = 本期应还 − 上期"（不计溢缴款） | 农行 MC 美元：1.41 ≠ 1.13；农行 VISA 美元：−581.28 ≠ −584.05 |

## 汇率
**目标**：报表里的"总支出"统一折算成人民币。每笔流水仍然保留原币种金额，折算只发生在汇总这一步。不追求精确。

- **哪一天的汇率**：用**账单邮件发出当天**的汇率，取邮件的 `Date` 头，也就是 `Bill.email_date`。同一份账单里的所有币种、所有流水都用这一天的汇率。
- **从哪里取**：[Frankfurter](https://api.frankfurter.dev)，欧洲央行参考汇率，免费、不需要 key，支持 CNY、USD、AUD、EUR，可以查历史日期；遇到周末和节假日，会自动返回上一个工作日的汇率。
  - 接口：`GET https://api.frankfurter.dev/v1/<YYYY-MM-DD>?base=<币种>&symbols=CNY`
  - 实测（2026-09-19）：2026-08-24 的 USD→CNY 为 6.7227，和农行账单上的购汇汇率 6.76 很接近，精度足够。
- **缓存**：取到的汇率写入 `fx_rates` 表，以后不再重复请求。
- **兜底**：联网失败时，用配置里的 `fx.fallback_to_cny`，并在报表里标注"配置汇率"。
- **计算**：本期各币种的支出合计 × 当天汇率，再相加得到人民币总额。月报按交易日归到自然月，但每笔金额仍按它所属账单的汇率折算。

## SQLite 表
开启 WAL、`busy_timeout` 和外键约束。

| 表 | 用途 | 关键约束 |
|---|---|---|
| `emails` | 原始邮件索引和处理状态 | `message_id` 唯一（缺失时用 `sha256`）；`status`、`attempts`、`last_error` |
| `folder_cursors` | IMAP 游标 | `(folder, uidvalidity)` → `last_uid` |
| `accounts` / `cards` | 账户和卡的元数据 | 账单日、还款日、授信额度、别名；用于缺账单心跳 |
| `bills` | 账单 | **`(bank, account_id, statement_date)` 唯一**；`status`、`reported_at`；`quality`（以后） |
| `bill_balances` | 每个币种的汇总块 | `(bill_id, currency)` 唯一 |
| `transactions` | 流水 | 按 `bill_id` 整体替换；包含 `synthetic`、`group_raw` |
| `category_rules` | 分类规则 | 也可以直接放在 YAML 里 |
| `notifications`（以后） | 待发送消息（outbox），第一版用 `bills.reported_at` 代替 | **`(bill_id, channel, kind)` 唯一** |
| `runs` | 每次运行的记录 | "运行中出错"告警要用；以后看门狗也要用 |
| `fx_rates` | 汇率缓存 | `(date, currency)` 唯一 → `rate_to_cny`、`source`（`frankfurter` 或 `config`） |

## 以后导出 Beancount 时怎么映射
- 卡 → `Liabilities:CreditCard:<银行>:<后四位>`（按币种分账户或加 `@` 价格）
- 分类 → `Expenses:…`
- `REPAYMENT` → 过渡账户 `Assets:Transfer`，避免和储蓄卡那边重复记账
- `FX_TRANSFER` → 同一张卡的两个币种账户之间转账，用 `fx_rate` 标价
- `REBATE` → `Income:Cashback`
- 合成调整 → `Expenses:Bank:Adjustment`
- 每期汇总块 → 在账单日加一条 `balance` 断言（净额 = 欠款 − 溢缴款）
