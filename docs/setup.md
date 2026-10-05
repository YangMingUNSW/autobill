# 操作手册（第一次配置）

这些步骤要你自己在银行 App、邮箱和电脑上操作，程序代替不了。下面以 iCloud 邮箱为例，程序也只在 iCloud 上实际用过。

## 1. 开通银行电子账单（每张卡都要做）
- 在各家银行的 App 或网银里确认已开通**邮件电子账单**，并且**邮件里带交易明细**（不能只发"账单已出"的通知）。
- **把电子账单的收件邮箱直接改成第 2 步那个 iCloud 别名**，让银行直接发过来，不要经过别的邮箱转发：
  - 收到的就是银行原件，发件人、签名、格式都不变，解析最稳；
  - 以后换邮箱只要改一次银行的设置，不用维护转发规则。
  - 先做完第 2 步（别名、文件夹、规则），再去银行改邮箱，第一封账单就会进 `AutoBill` 文件夹。
- 已知的发件人（写 iCloud 规则、排查问题时要用）：

  | 银行 | 发件人 |
  |---|---|
  | 农行 | `e-statement@creditcard.abchina.com.cn` |
  | 建行 | `service@vip.ccb.com` |
  | 中行 | `boczhangdan@bankofchina.com` |
  | 工行 | `webmaster@icbc.com.cn` |

## 2. 准备收账单的邮箱（iCloud）
用你 Apple ID 的 @icloud.com 邮箱就行，**不用注册新账号**，再加一个别名专门收账单。这个邮箱最好只给 AutoBill 用，原因见第 4 小步。

1. **加一个别名**：电脑浏览器打开 icloud.com → 邮件 → 设置 → 账户 → 添加别名，比如 `xxx.bills@icloud.com`。它专门收银行发来的账单，报表会发到主地址，两者分开。
2. **新建文件夹** `AutoBill`。**名字只用英文**：IMAP 里中文文件夹名要特殊编码，程序遇到中文名会直接提示你改。
3. **新建规则**：icloud.com → 邮件 → 设置 → 规则 → "发给"上面的别名 → 移到文件夹 `AutoBill`。原始账单就不会出现在收件箱里。
4. **生成 App 专用密码**：account.apple.com → 登录与安全 → App 专用密码 → 生成，名字写"AutoBill"。
   - 密码只显示一次。部署时放进环境变量 `AUTOBILL_IMAP_PASSWORD`，**不要写进任何文件或脚本，也不要发给任何人**（包括 AI 助手）。
   - **改 Apple ID 密码时，所有 App 专用密码都会失效**，程序会停。重新生成一个换上即可，`autobill check-mailbox` 会提示。
   - 这个密码能读写这个 Apple ID 的 iCloud 邮件、通讯录和日历，碰不到照片、云盘、钥匙串、付款，也改不了 Apple ID 密码。所以这个邮箱最好只给 AutoBill 用；万一密码泄露，在同一页一键吊销。
- **原件库**：程序对 `AutoBill` 文件夹**只读**，不删、不移动、不改已读状态。原始账单一直留在 iCloud 里，本机或服务器的数据丢了可以从这里重建。**不要手动删除**这个文件夹里的邮件。

## 3. 确认第一封账单到了
- 银行改好邮箱后，等下一期账单发出（或者在银行 App 里找"补发账单"一类的功能试试）。
- 然后按第 5 步运行 `check-mailbox`，看 `AutoBill` 文件夹里有没有它。
- 如果它进了 iCloud 的"垃圾邮件"：程序也会读"垃圾邮件"文件夹（只处理发给别名的邮件），不会漏；你也可以把它标成"不是垃圾邮件"，以后就不会再进去。
- 以前在别的邮箱设过把账单转发过来的规则，可以删掉，免得同一期账单进来两次（进来两次也没关系，程序会按账单去重）。

## 4. 历史账单（只做一次，手动）
- 以前的账单还在你原来的邮箱里（比如 QQ 邮箱）。搜索上面几个发件人，勾选想补进来的账单，用**"作为附件转发"**发到别名。程序会把附件里的每封原始账单拆出来单独处理。
- 一定要用"作为附件转发"，**不要用普通的"转发"**：普通转发会把账单正文改写成引用格式，程序解析不了。
- **分小批**转发，每次几封就好。一次勾选太多，QQ 会把附件变成"超大附件"，那是一个会过期的下载链接，程序拿不到原件。
- 重复转发没关系，程序会自动去重。
- **转发被退回时**（iCloud 有时把转发来的账单当成垃圾邮件拒收），可以从原来的邮箱把账单导出成 `.eml` 文件，放进一个文件夹直接导入：本机用 `uv run autobill import-dir <文件夹>`；Docker 里把文件夹放到 `data/` 下面，先 `docker compose stop`，再 `docker compose run --rm autobill import-dir /data/<文件夹>`，最后 `docker compose up -d`，下一轮会把这些月份的邮件补发给你。

## 5. 先在自己电脑上试运行
1. 从 `config.example.yaml` 复制出 `<数据目录>/config.yaml`，改这两节（地址换成你自己的）：

   ```yaml
   mail_fetcher:
     enabled: true
     imap_server: "imap.mail.me.com"
     imap_port: 993
     username: "你的主地址@icloud.com"
     folders: ["AutoBill", "Junk"]
     only_to: "xxx.bills@icloud.com"     # 上面那个别名：只处理发给它的邮件

   notifier:
     smtp_report:
       enabled: true
       smtp_server: "smtp.mail.me.com"
       smtp_port: 587
       security: "starttls"
       username: "你的主地址@icloud.com"
       to_addr: "你的主地址@icloud.com"  # 报表发给自己的主地址，会出现在收件箱
   ```

2. 设置密码（只对当前这个 PowerShell 窗口有效，关掉就没了）：

   ```powershell
   $env:AUTOBILL_IMAP_PASSWORD = "abcd-efgh-ijkl-mnop"   # 换成你的 App 专用密码
   ```

   收信和发信用同一个密码；发信会自动用它，不用再设 `AUTOBILL_SMTP_PASSWORD`。
3. `uv run autobill check-mailbox`：登录收信和发信，列出文件夹里有几封邮件，**不改动、不发送任何东西**。看到"全部正常"再往下。
4. `uv run autobill run --no-send`：拉取、解析，不发报表，先看解析结果对不对。
5. `uv run autobill run`：这次会把已经收齐的账单月邮件发到你的收件箱。再运行一次，应该什么都不发（不重复）。
6. 定时运行（每 30 分钟一次）放在服务器上，推荐用 Docker，见 [deploy.md](deploy.md)。服务器跑起来后，自己电脑上就不要再 `run` 了（`--no-send` 除外），否则报表会发两遍。

**如果某封账单显示 `UNRECOGNIZED`（不认识）或 `FAILED`（解析失败）**：程序已经记下读过它了，再运行也不会重读。程序修好、更新之后，下一次运行会自动重新解析它们（原件在数据目录的 `raw` 文件夹里，见 [pipeline.md](pipeline.md#运行层)）。想从头重读邮箱文件夹，运行 `uv run autobill run --rescan`：处理过的账单会跳过，之前失败或不认识的会重新处理。原始邮件一直在 iCloud 的文件夹里，也在数据目录的 `raw` 文件夹里，不会丢。
