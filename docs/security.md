# 安全：密钥、数据隔离与脱敏

对应代码和文件：`autobill/config.py`、`.gitignore`、`.gitattributes`、`.pre-commit-config.yaml`。

## 原则
代码、普通配置、密钥、数据**四者分离**。yaml 里不写任何密码。

## 密钥
- 放在 Windows 凭据管理器（`keyring`）或 `.env` 里；`.env` 在 Linux 上设成 chmod 600。
- 用 pydantic-settings 的 `SecretStr` 读取，打日志时显示为 `**********`。
- 需要的环境变量：
  - `AUTOBILL_IMAP_PASSWORD`：收信邮箱的密码。作者用的是 iCloud 的 **App 专用密码**
  - `AUTOBILL_SMTP_PASSWORD`：发信密码；**没设时自动用上面那个**（iCloud 收发是同一个邮箱）
  - `AUTOBILL_BOC_PDF_PASSWORD`：可选，实测中行 PDF 没有加密
  - `AUTOBILL_WECOM_SECRET`：以后接企业微信时使用
- 授权码一旦泄露，就去邮箱里撤销，再重新生成。
- **iCloud App 专用密码能做什么**（M8a，作者已确认接受）：读写这个 Apple ID 的 iCloud 邮件、通讯录、日历；**不能**碰照片、iCloud 云盘、钥匙串、付款，也不能改 Apple ID 密码。作者的这个 iCloud 邮箱只用于 AutoBill，所以泄露时能看到的只有账单和报表，外加通讯录和日历。泄露时在 account.apple.com 吊销；改 Apple ID 密码会让所有 App 专用密码失效。
- **M8a 的实现**：收信密码只从环境变量读取；`Mailbox` 的 `repr`、错误信息和命令输出都不含它（有测试）。
- **M7 的实现**：发信授权码只从环境变量 `AUTOBILL_SMTP_PASSWORD` 读取，不写进 `config.yaml`、日志、错误信息或任何文件；`Mailer` 的 `repr` 也不含它（有测试）。Windows 凭据管理器（`keyring`）在 M8 接计划任务时再加。

## 配置示例（`config.example.yaml`，入库；实际使用的 `config.yaml` 不入库）

**配置文件放在哪**：`<数据目录>/config.yaml`，也就是 `%LOCALAPPDATA%\autobill\config.yaml`；可以用环境变量 `AUTOBILL_CONFIG` 指到别处。**没有这个文件也能运行**，这时用和下面示例相同的默认值。M3 只读取 `fx` 一节，其余各节在用到它们的里程碑里接上。

**数据目录**：环境变量 `AUTOBILL_DATA_DIR` 优先（测试一律设成临时目录），否则是 `%LOCALAPPDATA%\autobill\`。

```yaml
system:
  timezone: "Asia/Shanghai"          # 账单日期按北京时间理解
  data_dir: null                     # null 表示用 platformdirs 的默认位置；测试时用 AUTOBILL_DATA_DIR
  backfill_months: 12

cards:
  card_aliases:                      # 同一账户的其他卡号（换卡、一户两卡）→ 记在哪个账户下
    "CCB:5678": "CCB:1234"

banks:                               # 按银行的策略，取值依据见 docs/banks/README.md
  ABC: { due_window_days: [15, 30] } # 以后再加：dkim: verify, zero_statement: unknown
  CCB: { due_window_days: [15, 30] } # 以后再加：dkim: none,   zero_statement: unknown
  BOC: { due_window_days: [15, 30] } # 以后再加：dkim: verify, zero_statement: always

mail_fetcher:
  imap_server: "imap.qq.com"
  imap_port: 993
  username: "bill_receiver@example.com"
  folders: ["INBOX", "CreditCard_Raw", "Junk"]
  poll_minutes: 15

fx:                                  # 折算人民币用的汇率，见 docs/data-model.md#汇率
  source: "https://api.frankfurter.dev/v1"   # 按账单邮件发出当天取汇率，免费、不需要 key
  fallback_to_cny: { USD: 7.10, AUD: 4.60, EUR: 7.80 }   # 联网失败时使用，报表里标注"配置汇率"

notifier:
  smtp_report:
    enabled: false                   # 填好真实邮箱后改成 true
    smtp_server: "smtp.qq.com"
    smtp_port: 465
    username: "bill_receiver@example.com"
    to_addr: "my_primary_mail@example.com"
  wechat_work:                       # 以后，经 Oracle 中转
    enabled: false
    relay_url: "http://10.66.66.1:8787"
    touser: "YOUR_USERID"            # 只填本人，不用 @all
```

## 数据隔离
- **数据目录放在仓库外**，用 platformdirs 定位，Windows 上是 `%LOCALAPPDATA%\autobill\`，里面有 `autobill.db`、`raw/` 原件缓存，以及 `fx_rates` 缓存。
- 测试用独立的 `AUTOBILL_DATA_DIR`。
- `.gitignore`（已建立）：`/samples/`、`*.local.yaml`、`.env`、`config.yaml`、`rules.yaml`、`/data/`、`*.db` 及其 `-wal`、`-shm` 文件、Python 缓存。
  - 规则以 `/` 开头，只匹配仓库根目录，避免误伤 `tests/data/` 之类的目录。
- `.gitattributes`（已建立）：`*.eml -text`、`*.pdf binary`。本机开着 `core.autocrlf=true`，不加这两条的话，提交时 Git 会改写样本的 CRLF 换行，样本的字节就变了。
- `.eml` **没有**全局排除，因为 `tests/fixtures/` 里的样本要入库。
- pre-commit（M0 建立，配置在 `.pre-commit-config.yaml`；CI 跑同一套）：
  - gitleaks；
  - 身份扫描 `scripts/check_identity.py`：拦截 18 位身份证号、11 位手机号、16 位以上的卡号；
  - `.eml` 和 `.pdf` **只允许出现在 `tests/fixtures/` 下**，`.db` 一律拦截。
- 身份扫描的几个要点：
  - **先解码再查**：`.eml` 按 MIME 解码，包括编码过的邮件头、套在里面的 `message/rfc822` 和 PDF 附件（按 `%PDF-` 识别）。只搜原始字节的话，base64 正文里的号码是看不到的。
  - **用校验位减少误报**：身份证号要通过末位校验码，卡号要通过 Luhn 校验。真号码一定能通过，Message-ID 这类随机数字串基本通不过。手机号没有校验位，11 位都算。
  - **卡号只认两种写法**：16–19 位连写，或 4 位一组（`6228 4812 …`）。中行明细里相邻的两个日期（`2025-06-02 2025-06-04`）连起来也是 16 位、有的还能通过 Luhn，所以不再把"用短横线隔开的任意数字"当卡号（2026-09-19）。
  - **报错时号码打码显示**：公开仓库的 CI 日志任何人都能看。
  - `uv.lock` 不查内容：它是工具生成的，满是哈希和文件大小，会误报。
  - `tests/test_check_identity.py` 用运行时拼出来的假号码，证明每条规则都**真的会报错**。
  - **本地私密词表**（2026-09-19 加）：卡号**后四位**、邮箱地址、姓名这类东西没有固定格式，规则拦不住（那天真把两个建行真实尾号写进了一个新分支，发现后改成占位号并重写了那个分支）。所以作者在数据目录放一个 `private-terms.txt`（每行一个；也可用环境变量 `AUTOBILL_PRIVATE_TERMS` 指到别处），检查时读它：
    - 纯数字的词只按完整数字匹配（前后不是字母和数字），交易编号、哈希里碰巧含有同样的数字不算；其他词不分大小写地匹配；
    - 报错只显示"第几个私密词"，不显示词本身；
    - 词表本身不进仓库；CI 上没有它，这一项只在作者本机提交时生效。
    - 作者的词表由脱敏对照表的 `replace` 和 `card_last4` 两类原值生成，再加上真实邮箱地址和新见到的卡号；`merchants_fixed` 和 `forbid_extra` 不放（那两类里有作者已接受公开的内容）。
- 日志里不打印邮件正文和金额明细。
- **从数据库往仓库里搬东西**只有一条路：`scripts/export_ai_categories.py`，把 AI 分好的商户导成规则。它只读；带支付渠道字样的名字（国内付给个人的消费，描述里有收款人真名）直接扣下，可能是人名的（两三个汉字，或带汉字且整条就是原始描述）留给作者自己判断。它**认不出拉丁字母写的人名**，所以默认输出仍要扫一眼再粘。判断依据和测试见 [notify.md](notify.md#把-ai-的结论沉淀成规则)。
- **数据库会作为附件离开服务器**：每月那封账单邮件带一份压缩的数据库，收件人只有作者自己配置的 `to_addr`（见 [notify.md](notify.md#每月备份)）。它落在作者自己的 iCloud 邮箱里，和银行原始账单待在同一个地方，没有引入新的暴露面；不想要就 `backup: {enabled: false}`。
- 数据库落盘加密靠 BitLocker，再加上文件权限；SQLCipher 作为以后的可选项。

## 样本与脱敏
**这是公开仓库。** 测试样本 [`tests/fixtures/`](../tests/fixtures/README.md) 是作者本人的真实账单，2026-09-18 **一次性脱敏**。项目里**没有**脱敏脚本：脱敏不是产品功能。当时用的工具和原件都存在作者本机的仓库外目录里。

**隐私边界：只清理身份信息**，包括姓名、住址、邮编、卡号（后四位统一换成 0001–0005，BIN 保留）、储蓄卡和分期账户尾号、邮箱和 QQ 号、手机号、证件号。消费记录、金额、日期、商户、额度都保持原样；金额本来也不能动，否则对账就不成立了。具体清单见样本 README。

### 以后加入新样本时的检查清单
新账单脱敏后、入库前，逐项检查：
1. **五个方面都要查**：
   - 邮件头：要先**解码**，编码过的中文姓名只有解码后才能看到；
   - HTML 源码：包括属性、链接、CSS；
   - 可见文字；
   - PDF 文字；
   - **PDF 嵌入字体**：ToUnicode 映射、字体内部的 cmap 表、没被使用却仍有轮廓的字形。
2. **不能只查"已知的"隐私**，还要用通用规则扫一遍：问候语里的姓名、所有掩码卡号和"尾号"、手机号、证件号、邮箱地址。新账单常常带着新的小店、新的卡号。
3. **按内容识别 PDF**：看开头是不是 `%PDF-`。附件名往往是编码过的中文，MIME 类型也可能是 `octet-stream`，按后缀或类型判断会漏。
4. 转发件里如果套着 `message/rfc822`，**里层原邮件的邮件头**也要处理。
5. 金额序列和表格行数必须和原件一致。
6. 文件本身不要改动格式；`.gitattributes` 会保证 Git 不改它们的换行。

### 教训（2026-09-18）
- **PDF 被整个漏掉**：按后缀判断附件类型漏掉了中行的 PDF，而校验用的是同一套判断，所以校验也一起漏了。
- **字体子集泄露**：PDF 里的文字删掉以后，嵌入字体里仍然保留着那些字的字形和 Unicode 映射，用字体工具就能读出被删掉的字。PyMuPDF 的 `subset_fonts()` 会跳过带子集前缀（`ABCDEF+`）的字体，所以要自己重新裁剪字体（保持字形编号不变），并重写 ToUnicode。
- **通用规则**：写检查时，要先拿一份"已知有问题的输出"去测，确认它真的能报错，然后才能用它来判断"没问题"。

### 使用样本时要注意
- 脱敏后 DKIM 签名必然失效，只能用来测"验证失败"这条分支，见 [parsing.md](parsing.md#测试)。
- 中行 PDF 里被替换的几处文字是重新写入的，字体和精确坐标与原件略有不同。
