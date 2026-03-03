# 🚀 部署指南 (Commercial)

本指南适用于当前仓库 `Limit-Up-Sniper-Commercial`。

---

## 🖥️ Windows（桌面版）

桌面启动脚本位于 `Desktop-Version/`。

1. 安装依赖与初始化

```bat
Desktop-Version\install.bat
```

2. 启动

```bat
Desktop-Version\run.bat
```

3. 更新

```bat
Desktop-Version\update.bat
```

默认访问地址：`http://127.0.0.1:8000`

---

## 🐧 Linux（服务器版，推荐）

服务器脚本位于 `Server-Version/`。

### 1) 一键安装

```bash
cd /opt
sudo git clone https://github.com/ForceMind/Limit-Up-Sniper-Commercial.git limit-up-sniper-commercial
cd limit-up-sniper-commercial
sudo bash Server-Version/install.sh
```

安装脚本会完成：
- Python 环境与依赖
- systemd 服务创建
- nginx 反向代理
- 数据目录初始化（包含龙虎榜映射文件）

### 2) 更新

```bash
cd /opt/limit-up-sniper-commercial
sudo bash Server-Version/update.sh
sudo journalctl -u limit-up-sniper-commercial -f
```

### 2.2) 脚本冲突合并原则（install/update）

近期 `install.sh` / `update.sh` 分支合并时，建议保留以下两类能力（不是二选一）：

- Python 兼容检测（`is_python_compatible` + `select_python_cmd`）
	- 作用：确保使用 Python 3.8+，避免低版本导致依赖安装或运行失败。
- worker 自适应（`calc_worker_count`）
	- 作用：根据 CPU 自动设置 worker 数，兼顾低配机器稳定性与高配机器吞吐。

二者职责不同：一个解决“能不能跑”，一个解决“跑得是否合理”，因此应同时保留。

`update.sh` 还应保留：
- 既有安装校验（`validate_existing_install`：目录 / service 文件 / venv）
- 虚拟环境修复（`ensure_venv`）

不建议保留 legacy 服务名兼容停止语句（例如 `systemctl stop limit-up-sniper`），以免误停错误实例。

### 2.1) 更新前兼容性自检（强烈建议）

```bash
# 1) 核对安装目录与服务文件
test -d /opt/limit-up-sniper-commercial && echo "APP_DIR OK"
test -f /etc/systemd/system/limit-up-sniper-commercial.service && echo "SERVICE FILE OK"

# 2) 核对 Python 虚拟环境
test -f /opt/limit-up-sniper-commercial/venv/bin/activate && echo "VENV OK"

# 3) 核对关键运行数据文件（缺失会影响登录/会员/日志）
ls -l /opt/limit-up-sniper-commercial/backend/data/{user_accounts.json,trial_fingerprints.json,referral_records.json,user_operation_logs.jsonl,seat_mappings.json,vip_seats.json}

# 4) 核对服务与 nginx 状态
sudo systemctl status limit-up-sniper-commercial --no-pager | head -n 12
sudo nginx -t
```

### 2.3) 冲突合并后快速验收

```bash
# 1) 确认无冲突标记
grep -R -nE '^(<<<<<<<|=======|>>>>>>>)' Server-Version/install.sh Server-Version/update.sh

# 2) 脚本语法检查（在 Linux 服务器执行）
bash -n Server-Version/install.sh
bash -n Server-Version/update.sh

# 3) 最小包脚本同步校验（可选）
sha256sum Server-Version/install.sh Minimal-Server-Deploy/Server-Version/install.sh
sha256sum Server-Version/update.sh Minimal-Server-Deploy/Server-Version/update.sh
```

说明：新版 `update.sh` 已固定为商业版路径与服务名，不再自动切换 legacy 服务，避免更新到错误实例。

### 3) 卸载

仅卸载服务与 nginx 配置：

```bash
cd /opt/limit-up-sniper-commercial
sudo bash Server-Version/uninstall.sh
```

连同应用目录一起删除：

```bash
cd /opt/limit-up-sniper-commercial
sudo bash Server-Version/uninstall.sh --remove-app
```

---

## 🧭 install / update / uninstall 怎么选

### 场景 A：首次部署（新服务器 / 新目录）
- 使用：`install.sh`
- 命令：`sudo bash Server-Version/install.sh`
- 说明：会创建 venv、systemd、nginx、并初始化必须数据文件。

### 场景 B：已是商业版目录 `/opt/limit-up-sniper-commercial`，仅升级代码
- 使用：`update.sh`
- 命令：`sudo bash Server-Version/update.sh`
- 说明：会备份并恢复运行数据，保留现有配置与账号数据。

### 场景 C：系统状态混乱（服务文件缺失 / venv 损坏 / 启动失败反复）
- 建议：先执行 `uninstall.sh`（不带 `--remove-app`），再重新 `install.sh`
- 若需“彻底重装”并清空历史数据，再使用 `uninstall.sh --remove-app` 后安装。

### 场景 D：未来新机器上线
- 一律走 `install.sh`。
- 旧机器日常迭代继续用 `update.sh`。

### 是否必须先卸载再升级？
- 不必须。正常升级优先 `update.sh`。
- 仅当环境损坏、端口/服务冲突长期无法修复时，再考虑卸载重装。

---

## 🔧 常用运维命令

```bash
# 服务状态
sudo systemctl status limit-up-sniper-commercial

# 重启服务
sudo systemctl restart limit-up-sniper-commercial

# 停止服务
sudo systemctl stop limit-up-sniper-commercial

# 查看实时日志
sudo journalctl -u limit-up-sniper-commercial -f

# 查看 nginx 配置检查
sudo nginx -t
```

---

## ❓ FAQ

### 1) 80 端口被占用
- 如果占用者是 nginx，安装脚本会复用该端口。
- 如果占用者不是 nginx，请先释放端口或改用其他监听端口。

### 2) 页面显示 WebSocket Disconnected
- 检查服务是否正常：`sudo systemctl status limit-up-sniper-commercial`
- 检查 nginx 是否正常：`sudo nginx -t && sudo systemctl restart nginx`

### 3) 龙虎榜数据文件缺失
- 当前安装/更新脚本会自动补齐 `seat_mappings.json` 与 `vip_seats.json`。

---

## 🛡️ 管理后台

- 访问地址：`http://你的域名或IP/admin/`
- 管理员 Token 文件：`backend/data/admin_token.txt`

功能包括：
1. 用户管理（增减时长）
2. 订单审核
3. 系统配置（含龙虎榜策略）
