# 部署到服务器（M8b）

**推荐用 Docker**（2026-09-19 定）：镜像由 GitHub Actions 自动构建，发布在 `ghcr.io/yangmingunsw/autobill`，同时有 x86（amd64）和 ARM（arm64）两种，Linux 服务器、群晖这类 NAS、苹果芯片的 Mac 都能跑。镜像里已经带好 Python、Chromium（打印 PDF）和中文字体，**服务器上只需要装 Docker**。

作者用的是一台 Oracle Cloud 免费服务器（Ubuntu 24.04，1 核 1 GB）。

**在服务器上跑起来以后，就不要再在自己电脑上运行 `autobill run` 了**（`--no-send` 除外），否则两边会各发一遍报表。

## 用 Docker（推荐）

### 1. 装 Docker
Ubuntu：

```bash
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER      # 之后重新登录一次，就不用每条命令都 sudo
```

其他系统见 Docker 官方文档；群晖在套件中心装 Container Manager。

### 2. 准备一个文件夹
```bash
mkdir -p ~/autobill-docker/data && cd ~/autobill-docker
curl -fsSLO https://raw.githubusercontent.com/YangMingUNSW/autobill/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/YangMingUNSW/autobill/main/config.example.yaml -o data/config.yaml
curl -fsSL https://raw.githubusercontent.com/YangMingUNSW/autobill/main/autobill.env.example -o autobill.env
chmod 600 autobill.env
```

文件夹里是这样的：

```
autobill-docker/
├── compose.yaml        # 怎么运行（每 30 分钟一次）
├── autobill.env        # 邮箱密码，只有你能读（600）
└── data/
    ├── config.yaml     # 你的配置
    └── ...             # 程序自己生成：数据库、原始邮件、标准账单
```

### 3. 填配置和密码
- `data/config.yaml`：改 `mail_fetcher` 和 `notifier.smtp_report` 两节，见 [setup.md 第 5 步](setup.md#5-运行m8a-手动m8b-放到服务器上定时运行)。
- `autobill.env`：`AUTOBILL_IMAP_PASSWORD=` 后面填邮箱密码（iCloud 用 App 专用密码），不加引号。

### 4. 先检查，再启动
```bash
docker compose run --rm autobill check-mailbox     # 应该"全部正常"
docker compose run --rm autobill run --no-send     # 看解析结果，不发邮件
docker compose up -d                               # 启动：立刻跑一次，之后每 30 分钟一次
```

### 日常
| 想做的事 | 命令（在 `~/autobill-docker` 里） |
|---|---|
| 看运行记录 | `docker compose logs --tail 50`（持续看：`-f`） |
| 看在不在运行 | `docker compose ps` |
| **更新到最新版** | `docker compose pull && docker compose up -d` |
| 退回某个版本 | 把 `compose.yaml` 里的 `:latest` 改成版本号（如 `:0.2.0`），再 `docker compose up -d` |
| 马上跑一次 | `docker compose restart`（重启后会立刻运行一次） |
| 程序修好后补处理失败的账单 | `docker compose run --rm autobill run --rescan` |
| 停止 / 启动 | `docker compose down` / `docker compose up -d` |
| 换了 App 专用密码 | 改 `autobill.env`，然后 `docker compose up -d` |
| 其他命令 | `docker compose run --rm autobill <命令>`，比如 `uncategorised`、`report --month 2026-09` |

- **数据都在 `data/` 里**：换镜像、更新版本都不会丢。备份就是复制这个文件夹。
- 容器以普通用户（uid 1000）运行，不是 root。
- 出问题时会发**提醒邮件**到你的邮箱（见 [notify.md](notify.md#提醒邮件)），不用盯着日志。只有"收信和发信用同一个密码，而这个密码失效了"时发不出提醒，这时看日志，或者注意到进度邮件不来了。
- 内存：`compose.yaml` 限制容器最多用 700 MB，给 1 GB 的服务器留余量；打印 PDF 时 Chromium 用得最多。

## 不用 Docker（备选）
直接在服务器上装 Python 环境，用 systemd 定时运行。文件在 `deploy/systemd/`。

```bash
sudo apt install -y git chromium fonts-noto-cjk      # Ubuntu 上 chromium 是 snap 版，1 GB 的机器建议改装 Google Chrome 的 .deb
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/YangMingUNSW/autobill.git ~/autobill
cd ~/autobill && uv sync --frozen
# 配置：~/.local/share/autobill/config.yaml；密码：~/.config/autobill.env（600）
set -a; . ~/.config/autobill.env; set +a
uv run autobill check-mailbox
sudo cp deploy/systemd/autobill.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now autobill.timer
```

日志用 `journalctl -u autobill -n 50`；更新用 `git pull && uv sync --frozen`。

## 其他
- 推荐同时装 `fail2ban`（`sudo apt install fail2ban`），把反复乱试 SSH 的 IP 自动拉黑。服务器只允许密钥登录，它们本来也进不来，只是让日志清净。
- **为什么 2026-09-19 先说不用 Docker、后来又改用**：一开始担心 1 GB 的机器构建和运行带浏览器的镜像太吃力。改成"镜像在 GitHub 上构建、服务器只下载运行"后，这个顾虑基本消失了：服务器上只多一个约 100 MB 常驻的 Docker。而对开源项目来说，别人能一条命令部署才是最重要的。
