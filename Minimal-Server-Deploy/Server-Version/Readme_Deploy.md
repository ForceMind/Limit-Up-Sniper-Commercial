# Limit-Up-Sniper Commercial（最小包）服务器部署说明

本目录脚本用于在 Linux 服务器部署与维护商业版服务，默认部署目录与 systemd 服务名如下：

- 部署目录：`/opt/limit-up-sniper-commercial`
- 服务名：`limit-up-sniper-commercial`

## 脚本说明

- `install.sh`：首次安装（安装依赖、创建 venv、部署 systemd/nginx、初始化数据目录）。
- `update.sh`：在保留现有环境与数据前提下更新代码与前端静态资源。
- `uninstall.sh`：卸载服务与 nginx 配置（默认不会删除业务数据，需按脚本提示确认）。
- `fix_server.sh`：常见环境问题一键修复。

## 快速开始

1. 进入脚本目录：

```bash
cd Server-Version
```

2. 首次安装：

```bash
sudo bash install.sh
```

3. 查看服务状态：

```bash
systemctl status limit-up-sniper-commercial --no-pager
```

## 日常更新

在应用目录执行更新脚本：

```bash
cd /opt/limit-up-sniper-commercial/Server-Version
sudo bash update.sh
```

更新后可检查：

```bash
systemctl status limit-up-sniper-commercial --no-pager
journalctl -u limit-up-sniper-commercial -n 100 --no-pager
```

## 配置与数据

- 后端配置：`/opt/limit-up-sniper-commercial/backend/data/config.json`
- nginx 配置：`/etc/nginx/sites-available/limit-up-sniper-commercial`
- systemd 配置：`/etc/systemd/system/limit-up-sniper-commercial.service`

## 合并冲突处理原则（install/update）

当 `install.sh` 或 `update.sh` 出现分支冲突时，使用**功能并集**策略，不做二选一：

- Python 兼容检测：保留 `is_python_compatible` + `select_python_cmd`
- worker 自适应：保留 `calc_worker_count`
- 更新健壮性：保留 `validate_existing_install` + `ensure_venv`
- 兼容清理：移除 legacy 停服语句 `systemctl stop limit-up-sniper || true`，避免误停旧服务名

## 冲突合并后快速验收

```bash
grep -R -nE '^(<<<<<<<|=======|>>>>>>>)' install.sh update.sh
bash -n install.sh
bash -n update.sh
```

若你同时维护主版与最小包，建议再做一致性校验：

```bash
python3 - << 'PY'
import hashlib, pathlib
pairs = [
    (pathlib.Path('/opt/limit-up-sniper-commercial/Server-Version/install.sh'), pathlib.Path('/opt/limit-up-sniper-commercial/Minimal-Server-Deploy/Server-Version/install.sh')),
    (pathlib.Path('/opt/limit-up-sniper-commercial/Server-Version/update.sh'), pathlib.Path('/opt/limit-up-sniper-commercial/Minimal-Server-Deploy/Server-Version/update.sh')),
]
for a,b in pairs:
    if a.exists() and b.exists():
        ha = hashlib.sha256(a.read_bytes()).hexdigest()
        hb = hashlib.sha256(b.read_bytes()).hexdigest()
        print(a.name, 'same=', ha==hb)
    else:
        print('missing:', a, b)
PY
```
