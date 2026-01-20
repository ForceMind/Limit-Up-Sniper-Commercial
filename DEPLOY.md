# 🚀 部署指南 (Deployment Guide)

本指南将指导你如何在 **Windows** 和 **Linux** 环境下部署 **Limit-Up Sniper**。

---

## 🖥️ Windows 部署 (本地运行)

适用于个人电脑或 Windows 服务器。

### 1. 环境准备
*   确保已安装 [Python 3.8+](https://www.python.org/downloads/)。
*   确保已安装 [Git](https://git-scm.com/downloads)。

### 2. 获取代码
打开 PowerShell 或 CMD：
```bash
git clone https://github.com/ForceMind/Limit-Up-Sniper.git
cd Limit-Up-Sniper
```

### 3. 一键安装
双击运行项目根目录下的 `install.bat`。
*   脚本会自动创建虚拟环境。
*   自动安装所有依赖。
*   提示你输入 **Deepseek API Key** 并保存配置。

### 4. 启动服务
双击运行 `run.bat`。
*   服务启动后，浏览器访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

### 5. 更新代码
双击运行 `update.bat`。
*   自动拉取最新代码并更新依赖。


## 🐧 Linux 部署 (服务器)

适用于 **Ubuntu 20.04/22.04 LTS**, **CentOS 7+**, **Alibaba Cloud Linux 3**。

### ⚡ 一键部署 (推荐)

我们提供了一个自动化脚本，可以帮你完成所有安装步骤 (Python, Nginx, Systemd)。

1.  **下载代码**
    ```bash
    cd ~
    git clone https://github.com/ForceMind/Limit-Up-Sniper.git limit-up-sniper
    cd limit-up-sniper
    ```

2.  **运行安装脚本**
    ```bash
    sudo bash install.sh
    ```

3.  **按提示操作**
    *   脚本会自动安装系统依赖。
    *   当提示输入 **Deepseek API Key** 时，请粘贴你的密钥。
    *   当提示输入 **IP 或域名** 时，确认即可。

4.  **完成**
    *   脚本运行结束后，直接访问显示的 URL 即可使用。

### 🔄 如何更新 (Update)

运行更新脚本，它会自动拉取最新代码、更新依赖并重启服务。

```bash
cd Limit-Up-Sniper
sudo systemctl stop limit-up-sniper
sudo bash update.sh
sudo journalctl -u limit-up-sniper -f
```

---

## 🛠️ 手动部署 (Linux Manual)

如果你想手动控制每一个步骤，请参考以下流程。

### 1. 环境准备
```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv git nginx -y
```

### 2. 配置 Python 环境
```bash
cd Limit-Up-Sniper
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置 Systemd 服务
创建服务文件 `/etc/systemd/system/limit-up-sniper.service`：

```ini
[Unit]
Description=Limit-Up Sniper FastAPI Service
After=network.target

[Service]
User=root
WorkingDirectory=/root/limit-up-sniper
Environment="PATH=/root/limit-up-sniper/venv/bin"
Environment="DEEPSEEK_API_KEY=sk-你的密钥"
ExecStart=/root/limit-up-sniper/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable limit-up-sniper
sudo systemctl restart limit-up-sniper
```

### 4. 配置 Nginx 反向代理
创建配置文件 `/etc/nginx/sites-available/limit-up-sniper`：

```nginx
server {
    listen 80;
    server_name your_server_ip;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

启用配置：
```bash
sudo ln -sf /etc/nginx/sites-available/limit-up-sniper /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```
## 常用维护命令

*   **查看应用日志**:
    ```bash
    sudo journalctl -u limit-up-sniper -f
    ```
*   **重启应用**:
    ```bash
    sudo systemctl restart limit-up-sniper
    ```
*   **停止应用**:
    ```bash
    sudo systemctl stop limit-up-sniper
    ```
*   **更新代码**:
    ```bash
    cd ~/limit-up-sniper
    git pull
    sudo systemctl restart limit-up-sniper
    ```

## 2. 上传代码
将整个 `Limit-Up-Sniper` 文件夹上传到服务器。

## 3. 安装依赖
```bash
cd Limit-Up-Sniper
pip install -r requirements.txt
```

## 4. 设置环境变量 (Deepseek Key)
```bash
export DEEPSEEK_API_KEY="your-key-here"
```

## 5. 后台运行 (使用 nohup)
```bash
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 > server.log 2>&1 &
```

## 6. 访问
在浏览器访问 `http://服务器IP:8000`。

## 7. (可选) 使用 Nginx 反向代理
如果需要绑定域名或使用 80 端口，建议配置 Nginx。

## 8. 停止服务
如果使用 nohup 运行，请使用以下命令停止：
```bash
pkill -f uvicorn
```
或者先查找进程 ID 再停止：
```bash
ps -ef | grep uvicorn
kill <PID>
```
---

## ❓ 常见问题 (FAQ)

### 1. 启动报错 "ModuleNotFoundError"
*   **原因**: 依赖未安装或虚拟环境未激活。
*   **解决**: 运行 `install.bat` (Windows) 或 `pip install -r requirements.txt` (Linux)。

### 2. 页面显示 "WebSocket Disconnected"
*   **原因**: Nginx 未正确配置 WebSocket 转发，或服务未启动。
*   **解决**: 检查 Nginx 配置中的 `/ws` 部分，或检查后端日志 `sudo journalctl -u limit-up-sniper -f`。

### 3. 数据不更新
*   **原因**: 可能是非交易时间，或新浪接口访问受限。
*   **解决**: 检查服务器时间是否正确，或查看后台日志是否有报错。


##  管理后台 (Admin Panel)

本系统包含一个管理员后台，用于管理用户、订单和系统配置。

*   **访问地址**: `http://你的域名或IP/admin/`
*   **初始密码**: 系统首次启动时会自动生成一个 Admin Token。
    *   查看位置: `backend/data/admin_token.txt`
    *   请妥善保管此 Token。

### 功能说明
1.  **用户管理**: 查看注册设备，手动增加用户时长 (Add Time)。
2.  **订单管理**: 审核用户的支付订单 (Code验证)。
3.  **系统配置**: 修改龙虎榜同步策略、交易时间段等。
