# 操作手册（第一次配置）

这些步骤都要**你本人**在邮箱、银行 App 和电脑上操作，程序代替不了。以 QQ 邮箱为例；163 邮箱的差异单独注明。

## 1. 开通银行电子账单（每张卡都要做）
- 在各家银行的 App 里确认已开通**邮件电子账单**，收件地址是你的主邮箱，并且**邮件里带交易明细**（不能只发"账单已出"的通知）。
- 已知的发件人（主邮箱做转发规则时要用）：

  | 银行 | 发件人 |
  |---|---|
  | 农行 | `e-statement@creditcard.abchina.com.cn` |
  | 建行 | `service@vip.ccb.com` |
  | 中行 | `boczhangdan@bankofchina.com` |

## 2. 中心邮箱 = 你的 iCloud 邮箱（2026-09-19 定）
你的 Apple ID 下有一个**只用于 AutoBill 的 @icloud.com 邮箱**，直接拿它当中心邮箱，**不用注册任何新账号**。

1. **加一个别名**：电脑浏览器打开 icloud.com → 邮件 → 设置 → 账户 → 添加别名，比如 `xxx.bills@icloud.com`。它专门接收 QQ 转发来的账单，报表会发到主地址，两者分开。
2. **新建文件夹** `AutoBill`。**名字只用英文**：IMAP 里中文文件夹名要特殊编码，程序遇到中文名会直接提示你改。
3. **新建规则**：icloud.com → 邮件 → 设置 → 规则 → "发给"上面的别名 → 移到文件夹 `AutoBill`。原始账单就不会出现在收件箱里。
4. **生成 App 专用密码**：account.apple.com → 登录与安全 → App 专用密码 → 生成，名字写"AutoBill"。
   - 密码只显示一次。部署时放进环境变量 `AUTOBILL_IMAP_PASSWORD`，**不要写进任何文件或脚本，也不要发给任何人**（包括 AI 助手）。
   - **改 Apple ID 密码时，所有 App 专用密码都会失效**，程序会停。重新生成一个换上即可，`autobill check-mailbox` 会提示。
   - 这个密码能读写这个 Apple ID 的 iCloud 邮件、通讯录和日历，碰不到照片、云盘、钥匙串、付款，也改不了 Apple ID 密码。作者已确认可以接受：这个邮箱只用于 AutoBill（2026-09-19）。万一泄露，在同一页一键吊销。
- **原件库**：程序对 `AutoBill` 文件夹**只读**，不删、不移动、不改已读状态。原始账单一直留在 iCloud 里，本机或服务器的数据丢了可以从这里重建。**不要手动删除**这个文件夹里的邮件。

## 3. QQ 主邮箱设置自动转发（新账单）
- 在 QQ 主邮箱新建收信规则：**发件人是上表地址之一** → 自动转发到**上面那个别名**。
- **只按银行发件人转发**，不要转发所有邮件。
- 设置好以后，等一封真实账单被转发过去（或者手动转发一封试试），然后按第 5 步运行 `check-mailbox`，看 `AutoBill` 文件夹里有没有它。
- 如果它进了 iCloud 的"垃圾邮件"：程序也会读"垃圾邮件"文件夹（只处理发给别名的邮件），不会漏；你也可以把它标成"不是垃圾邮件"。

## 4. 历史账单（只做一次）
- 在主邮箱网页版搜索上面三个发件人，勾选**最近 12 个月**的账单邮件，用"**作为附件**转发"发到上面那个别名。程序会把附件里的每封原始账单拆出来单独处理。
- **分小批**转发，每次几封就好。一次勾选太多，QQ 会把附件变成"超大附件"，那是一个会过期的下载链接，程序拿不到原件。
- 重复转发没关系，程序会自动去重。

## 5. 运行（M8a 手动；M8b 放到服务器上定时运行）
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
5. `uv run autobill run`：这次会把进度邮件发到你的 iCloud 收件箱。再运行一次，应该什么都不发（不重复）。
6. 定时运行（每 30 分钟一次）在 M8b 放到 Oracle 服务器上，见 [pipeline.md](pipeline.md#部署)。

## 6. 企业微信（以后，可选）
1. 注册企业微信，不需要认证，企业里只有你自己。
2. 创建自建应用，**可见范围只勾选你自己**；记下 corpid、agentid、secret。
3. 在企业微信的"微信插件"里用个人微信扫码关注，之后消息会进入个人微信。
4. 在 Oracle 服务器上：
   - 申请**固定公网 IP**；
   - 部署中转服务；
   - 用"接收消息服务器 URL"完成验证，再把固定 IP 填进"企业可信 IP"。
5. 家里电脑加一个 WireGuard peer，连到 Oracle。
