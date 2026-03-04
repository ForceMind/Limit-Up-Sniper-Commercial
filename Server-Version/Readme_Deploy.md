# 涨停狙击手商业版 - 服务器部署指南

## 1. 前置条件
- 系统：Ubuntu 20.04+/Debian 11+/CentOS 7+/Alibaba Cloud Linux
- 端口：开放 `22`、`80`
- 权限：`root` 或可 `sudo`

## 2. 安装（首次部署）
```bash
cd /opt
sudo git clone https://github.com/ForceMind/Limit-Up-Sniper-Commercial.git limit-up-sniper-commercial
cd /opt/limit-up-sniper-commercial
sudo bash Server-Version/install.sh
```

安装后关键对象：
- 安装目录：`/opt/limit-up-sniper-commercial`
- 服务名：`limit-up-sniper-commercial`
- service 文件：`/etc/systemd/system/limit-up-sniper-commercial.service`

## 3. 更新（已有部署）
```bash
cd /opt/limit-up-sniper-commercial
sudo bash Server-Version/update.sh
```

## 4. 健康检查
```bash
sudo systemctl status limit-up-sniper-commercial --no-pager | head -n 12
curl -i http://127.0.0.1:8001/api/status
sudo nginx -t
```

## 5. 脚本冲突合并建议（install/update）
建议同时保留以下能力：
- Python 兼容检测：`is_python_compatible` + `select_python_cmd`（保证 Python 3.8+）
- worker 自适应：`calc_worker_count`（按 CPU 自动设置）

并保留：
- `validate_existing_install`（目录/service/venv 校验）
- `ensure_venv`（旧环境重建）

不建议保留：
- 旧服务名兼容停止语句（如 `systemctl stop limit-up-sniper`），避免误停其他实例。

## 6. 冲突合并后验收
```bash
# 无冲突标记
grep -R -nE '^(<<<<<<<|=======|>>>>>>>)' Server-Version/install.sh Server-Version/update.sh

# 语法检查
bash -n Server-Version/install.sh
bash -n Server-Version/update.sh
```

## 7. 常用运维
```bash
sudo journalctl -u limit-up-sniper-commercial -f
sudo systemctl restart limit-up-sniper-commercial
sudo systemctl stop limit-up-sniper-commercial
```

### 终端运维面板（推荐）
安装或更新后可直接使用命令：
```bash
zt
```

`zt` 面板支持：
- 启动/重启/停止后端服务
- 启动/停止前端（Nginx）
- 健康检查（内外网 status）
- 查看/跟踪后端与 Nginx 日志
- 参数设置（Worker、状态接口限流、后台访问路径、开机自启）

## 8. 卸载
```bash
cd /opt/limit-up-sniper-commercial
sudo bash Server-Version/uninstall.sh
# 或彻底删除
sudo bash Server-Version/uninstall.sh --remove-app
```
