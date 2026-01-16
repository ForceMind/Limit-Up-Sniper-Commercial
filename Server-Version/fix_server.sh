#!/bin/bash
# fix_server.sh - 修复服务器 WebSocket 连接和配置问题

echo "正在修复服务器配置..."

# 1. 确保使用单进程模式 (修复无日志问题)
SERVICE_FILE="/etc/systemd/system/limit-up-sniper.service"
if grep -q "workers 4" "$SERVICE_FILE"; then
    echo "检测到多进程配置，正在修正为单进程..."
    sed -i 's/workers 4/workers 1/g' "$SERVICE_FILE"
    systemctl daemon-reload
fi

# 2. 确保 Nginx 配置正确 (修复 WebSocket 连接失败)
# 有时候 Nginx 配置没有正确加载，或者缺少 Upgrade 头
NGINX_CONF="/etc/nginx/sites-available/limit-up-sniper"

# 重新写入标准的 Nginx 配置
SERVER_IP=$(curl -s ifconfig.me || echo "_")
cat > $NGINX_CONF <<EOF
server {
    listen 80;
    server_name $SERVER_IP;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 86400; # 防止 WebSocket 超时断开
    }
}
EOF

# 3. 重启所有服务
echo "重启服务中..."
systemctl restart limit-up-sniper
ln -sf $NGINX_CONF /etc/nginx/sites-enabled/
nginx -t && systemctl restart nginx

echo "✅ 修复完成！请刷新浏览器重试。"
