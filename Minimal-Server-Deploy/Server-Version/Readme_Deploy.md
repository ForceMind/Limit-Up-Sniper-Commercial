# Limit-Up Sniper Commercial Deployment Guide
# 涨停狙击手商业版 - 服务器部署指南

## 1. 准备工作 (Prerequisites)

- **服务器**: 一台云服务器 (阿里云/腾讯云/AWS等)
- **操作系统**: 推荐 Ubuntu 20.04/22.04 LTS 或 CentOS 7/8 (Alibaba Cloud Linux 3)
- **最低配置**: 1核 CPU, 2GB 内存 (建议 2核 4GB 以获得更好性能)
- **开放端口**: 确保安全组开放 **80 (HTTP)** 和 **22 (SSH)** 端口

## 2. 上传代码 (Upload Code)

使用 SFTP 工具 (如 WinSCP, FileZilla) 或 `scp` 命令将整个项目上传到服务器的 `~` (主目录) 或 `/tmp` 目录。
推荐上传除了 `.venv` 和 `__pycache__` 以外的所有文件。

示例 (在本地终端执行):
```bash
# 假设你的服务器IP是 1.2.3.4，用户是 root
scp -r Limit-Up-Sniper-Commercial root@1.2.3.4:/root/
```

## 3. 执行安装 (Run Installation)

SSH 登录到服务器，进入上传的项目目录，给予脚本执行权限并运行。

```bash
# 1. 进入目录
cd /root/Limit-Up-Sniper-Commercial

# 2. 赋予脚本执行权限
chmod +x Server-Version/install.sh

# 3. 运行安装脚本 (必须使用 sudo 或 root)
# 脚本会自动识别您的系统 (Ubuntu/CentOS/Aliyun) 并安装 Python, Nginx 等依赖
sudo ./Server-Version/install.sh
```

**安装过程中:**
- 脚本会自动安装系统依赖 (git, python3, nginx等)。
- 脚本会自动配置 Python 虚拟环境并安装依赖。
- **提示**: 脚本会询问您的服务器 IP 或域名，直接回车使用自动检测的 IP 即可。
- **注意**: 如果您已经有 Nginx 在运行，脚本会通过反向代理配置覆盖 `/etc/nginx/sites-enabled/limit-up-sniper`，通常不会影响其他站点，除非端口冲突。

## 4. 验证部署 (Verify)

安装完成后，脚本会输出访问地址。

- **前台地址**: `http://你的服务器IP/`
- **后台管理**: `http://你的服务器IP/admin/index.html`

如果无法访问，请检查云服务器的安全组设置，确保 **80 端口** 已对外开放。

## 5. 日后维护 (Maintenance)

### 查看日志
```bash
# 查看后端服务实时日志
sudo journalctl -u limit-up-sniper -f
```

### 更新代码
如果本地修改了代码，重新上传覆盖后，运行更新脚本：
```bash
# 赋予更新脚本权限
chmod +x /opt/limit-up-sniper/scripts/update.sh

# 运行更新 (指向你的源码目录)
sudo /opt/limit-up-sniper/scripts/update.sh /root/Limit-Up-Sniper-Commercial
```

### 重启服务
```bash
sudo systemctl restart limit-up-sniper
```

## 常见问题 (FAQ)

**Q: 为什么日志显示 "ModuleNotFoundError"?**
A: 可能是依赖包未安装完全。尝试手动进入环境安装:
```bash
cd /opt/limit-up-sniper
source venv/bin/activate
pip install -r backend/requirements.txt
sudo systemctl restart limit-up-sniper
```

**Q: Nginx 启动失败?**
A: 检查 Nginx 配置: `sudo nginx -t`. 如果 80 端口被占用，请停止占用进程或修改 `/etc/nginx/conf.d/limit-up-sniper.conf` 中的端口。
