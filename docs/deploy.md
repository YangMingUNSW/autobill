# 部署到 Linux 服务器（M8b）

对应文件：`deploy/systemd/autobill.service`、`deploy/systemd/autobill.timer`。作者用的是一台 Oracle Cloud 免费服务器（Ubuntu 24.04，1 核 1 GB）；别的 Linux 服务器同理。

**在服务器上跑起来以后，就不要再在自己电脑上运行 `autobill run` 了**（`--no-send` 除外），否则两边会各发一遍报表。

## 服务器上要有的东西
- Python 3.12（Ubuntu 24.04 自带）、`git`、[`uv`](https://docs.astral.sh/uv/)（装在 `~/.local/bin`）。
- **Google Chrome**（打印标准账单 PDF 用）：用 Google 官方的 `.deb` 源安装，不要用 snap 版 chromium（1 GB 内存的机器上 snap 太重）。
- **中文字体** `fonts-noto-cjk`：不装的话，PDF 里的中文会变成方框。
- 可选但推荐：`fail2ban`，把反复试 SSH 密码的 IP 自动拉黑（服务器只能用密钥登录，它们本来也进不来，只是让日志清净）。

```bash
sudo apt install -y git fonts-noto-cjk fail2ban
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/YangMingUNSW/autobill.git ~/autobill
cd ~/autobill && uv sync --frozen
```

## 一次性设置
1. **配置文件** `~/.local/share/autobill/config.yaml`：从仓库里的 `config.example.yaml` 复制，填好 `mail_fetcher`、`notifier.smtp_report` 和（需要的话）`cards.card_aliases`。见 [setup.md 第 5 步](setup.md#5-运行m8a-手动m8b-放到服务器上定时运行)。
2. **密码文件** `~/.config/autobill.env`，**只有你自己能读**：

   ```bash
   install -m 600 /dev/null ~/.config/autobill.env   # 已经有了就跳过
   nano ~/.config/autobill.env
   ```

   里面写一行（换成你的 App 专用密码，不要引号）：

   ```
   AUTOBILL_IMAP_PASSWORD=abcd-efgh-ijkl-mnop
   ```

3. **先手动检查一次**：

   ```bash
   cd ~/autobill
   set -a; . ~/.config/autobill.env; set +a     # 把密码读进当前窗口
   uv run autobill check-mailbox                # 应该"全部正常"
   uv run autobill run --no-send                # 看解析结果
   ```

4. **装上定时运行**：

   ```bash
   sudo cp ~/autobill/deploy/systemd/autobill.* /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now autobill.timer
   sudo systemctl start autobill.service        # 马上跑一次，不等 30 分钟
   ```

## 日常
| 想做的事 | 命令 |
|---|---|
| 看下一次什么时候跑 | `systemctl list-timers autobill` |
| 看最近几次的输出 | `journalctl -u autobill -n 50 --no-pager` |
| 马上跑一次 | `sudo systemctl start autobill.service` |
| 暂停 / 恢复定时运行 | `sudo systemctl disable --now autobill.timer` / `enable --now` |
| 更新程序 | `cd ~/autobill && git pull && uv sync --frozen`（下次定时运行就是新版本） |
| 程序修好后补处理失败的账单 | `set -a; . ~/.config/autobill.env; set +a; uv run autobill run --rescan` |
| 换了 App 专用密码 | 改 `~/.config/autobill.env`，不用重启任何东西 |

出问题时不用盯着日志：收信失败、账单解析失败、不认识的邮件、新卡号，都会发**提醒邮件**到你的 iCloud（见 [notify.md](notify.md#提醒邮件)）。只有"收信和发信用的是同一个密码，而这个密码失效了"时发不出提醒，这时只能看日志，或者注意到进度邮件不来了。

## 为什么不用 Docker
2026-09-19 讨论过：这台机器只有 1 GB 内存，Docker 守护进程和带 Chrome 的镜像（约 1 GB）都是负担，Chrome 在容器里打印 PDF 还要额外调共享内存和权限；而直接部署只需要上面几条命令和两个 systemd 文件，更新是 `git pull`。以后要搬到别的机器（NAS、Mac）或者给别人一键部署时，再加 Dockerfile，代码不用改。
