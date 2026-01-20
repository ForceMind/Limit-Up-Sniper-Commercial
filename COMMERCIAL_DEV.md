# 涨停狙击手 - 商业版开发文档 (Commercial Version Developer Guide)

本文档详细说明了“涨停狙击手”商业版本的架构、功能逻辑、安全机制及运维部署注意事项。

## 1. 系统架构 (System Architecture)

系统采用前后端分离架构，但针对商业部署进行了特殊优化。

*   **后端 (Backend)**: Python FastAPI。负责核心逻辑、数据分析、用户鉴权、订单处理及邮件通知。
*   **前端 (Frontend)**: 原生 HTML/JS/TailwindCSS (无构建步骤)。
    *   **用户端**: `frontend/index.html`。可分发给用户本地运行，通过配置连接远程服务器。
    *   **管理端**: `frontend/admin/index.html`。**必须部署在服务器端**，通过后端静态文件服务托管，确保安全性。

### 部署拓扑
*   **Server**: 运行 FastAPI 后端，托管 Admin 前端。
*   **Client**: 用户可在浏览器直接访问 Server 页面，或下载 Frontend 文件在本地运行 (需修改 `config.js` 指向 Server IP)。

## 2. 核心功能逻辑

### 2.1 用户体系 (User System)
*   **识别方式**: 基于 `Device-ID` (设备指纹)。用户无需注册账号密码，首次访问自动创建。
*   **权限等级**:
    *   `Trial` (体验版): 3天有效期，功能受限。
    *   `Basic` (基础版): 解锁即时AI分析。
    *   `Advanced` (进阶版): 解锁竞价抢筹、尾盘低吸。
    *   `Flagship` (旗舰版): 全功能解锁，最高频次限制。
*   **配额管理**: 每日限制 AI 分析次数、策略扫描次数，每日凌晨自动重置。

### 2.2 支付与订单 (Payment & Order)
1.  **创建订单**: 用户选择版本和时长，后端生成唯一的 `order_code` (24位混合字符)。
2.  **支付**: 用户扫描二维码 (线下支付，如支付宝/微信个人码)。
3.  **确认支付**: 用户点击“我已支付”，订单状态变为 `waiting_verification`。
4.  **邮件通知**: 系统通过 SMTP 发送邮件给管理员，包含订单号和金额。
5.  **人工审核**: 管理员在后台核对到账情况，点击“通过”。
6.  **权益发放**: 系统自动计算过期时间 (当前时间/原过期时间 + 购买时长)。

### 2.3 升级/续费逻辑 (Upgrade/Renewal)
*   **场景**: 用户在有效期内购买更高版本 (如 Basic -> Advanced)。
*   **折算算法**:
    1.  计算当前版本剩余价值 (`remaining_minutes * current_version_price_per_minute`)。
    2.  根据目标版本价格，将剩余价值折算为新的时长 (`value / target_version_price_per_minute`)。
    3.  总时长 = 购买时长 + 折算时长。
*   **结果**: 用户立即升级到新版本，有效期相应延长或缩短 (视差价而定)。

### 2.4 后台管理 (Admin Panel) (必须同源)
*   **安全认证**: 基于 `X-Admin-Token`。Token 首次运行生成于 `backend/data/admin_token.txt`。
*   **功能**:
    *   用户列表与状态查看。
    *   **手动加时**: 可针对特定用户手动增加时长 (用于补偿或赠送)。
    *   订单审核: 批准/拒绝。
    *   系统配置: 调整邮件 SMTP、LHB 抓取设置、定时任务等。

## 3. 安全与防护 (Security)

1.  **CORS 策略**: 
    *   API 允许 `*` (为支持用户本地客户端连接)。
    *   **关键**: 管理后台页面建议仅通过服务器地址访问，不分发给用户。
2.  **订单防撞库**: 订单号升级为 24 位汉字+数字+字母混合乱序字符串，极难通过暴力猜测碰撞。
3.  **邮件通知**: 使用 SSL 加密传输，确保管理员第一时间获知支付动态。

## 4. 部署指南

### 4.1 服务器配置
1.  运行 `Server-Version/install.sh` 安装依赖。
2.  配置 `backend/data/config.json` (首次运行生成，需在后台填入 SMTP 信息)。
3.  启动服务: `python backend/app/main.py` 或使用 Supervisor/Systemd 托管。

### 4.2 客户端配置
若用户本地运行，需编辑 `frontend/config.js`:
```javascript
const API_BASE_URL = "http://YOUR_SERVER_IP:8000"; // 指向远程服务器
```

### 4.3 常见问题
*   **邮件发不出**: 检查云服务器防火墙是否放行 465 端口 (阿里云/腾讯云默认封禁 25，需用 465 SSL)。
*   **静态资源 404**: 确保 `frontend` 目录与 `backend` 目录层级正确。

## 5. 项目结构说明

*   `backend/app/api/admin.py`: 包含手动加时 (`/users/add_time`) 和 升级折算逻辑。
*   `backend/app/core/purchase_manager.py`: 包含 24位 复杂订单号生成逻辑。
*   `backend/data/`: 存储所有状态数据 (SQLite + JSON)，需定期备份。
