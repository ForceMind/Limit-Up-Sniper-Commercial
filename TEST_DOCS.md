# 涨停狙击手商业版 - 测试文档 (Test Documentation)

本文件详细描述了系统的功能测试点、API 接口清单及模拟测试流程。

## 1. 测试环境 (Test Environment)
*   **Backend**: Python 3.9+ (FastAPI)
*   **Database**: SQLite (`backend/data/stock_ai.db`)
*   **Key Files**: `backend/data/config.json`, `backend/data/admin_token.txt`

## 2. 自动化模拟测试 (Automated Simulation)
我们提供了一个 Python 脚本用于在虚拟环境中模拟完整的用户生命周期，验证核心商业逻辑的闭环。

**脚本路径**: `tests/simulate_full_flow.py`

**覆盖场景**:
1.  **新用户注册**: 验证根据 `Device-ID` 自动创建 Trial 账户。
2.  **下单流程**: 验证价格计算、订单创建API。
3.  **支付确认**: 模拟客户端发送“已支付”信号。
4.  **后台审核**: 模拟管理员获取 Token，查询待审核订单，并执行批准操作。
5.  **权益交付**: 验证用户版本是否变更为 `Paid Version`，过期时间是否正确延长。
6.  **人工加时**: 验证管理员手动给用户增加时长的功能。

**运行方式**:
```bash
python tests/simulate_full_flow.py
```

## 3. 功能测试矩阵 (Functional Test Matrix)

| 模块 | 测试点 | 预期结果 | 接口 (API) |
| :--- | :--- | :--- | :--- |
| **用户认证** | 首次访问 | 自动创建账户，版本为 Trial，初始配额正常 | `POST /api/auth/login` |
| | 二次访问 | 返回相同账户信息，无数据丢失 | `POST /api/auth/login` |
| | 过期检查 | 每日配额扣除后，再请求应被拦截 (400/403) | Middleware Logic |
| **支付交易** | 价格计算 | `1m/3m/1y` 对应的价格与配置文件一致 | `GET /api/payment/pricing` |
| | 创建订单 | 返回唯一的 24位 订单号，状态 Pending | `POST /api/payment/create_order` |
| | 确认支付 | 状态变为 Waiting，触发邮件通知 | `POST /api/payment/confirm_payment` |
| **后台管理** | 鉴权 | 无 Token 或 错误 Token 拒绝访问 (403) | `Header: X-Admin-Token` |
| | 订单列表 | 能看到所有状态的订单，支持过滤 | `GET /api/admin/orders` |
| | 审批通过 | 订单变 Completed，用户权益即时生效 | `POST /api/admin/orders/approve` |
| | 系统配置 | 修改 SMTP/LHB 设置后，文件落地保存 | `POST /api/admin/config` |
| | 用户列表 | 显示所有用户及当前的剩余配额 | `GET /api/admin/users` |
| | 手动加时 | 指定用户增加任意分钟数，过期时间顺延 | `POST /api/admin/users/add_time` |
| **核心业务** | 个股分析 | 调用 AI 分析接口，扣除 `ai` 配额 | `POST /api/analyze/stock` |
| | LHB 数据 | 调用数据同步，数据落地到 CSV/DB | `POST /api/lhb/fetch` |

## 4. 接口清单 (API Manifest)

### Auth
*   `POST /api/auth/login`: 登录/注册 (Body: {"device_id": "..."})

### Payment
*   `GET /api/payment/pricing`: 获取价格表
*   `POST /api/payment/create_order`: 创建支付订单
*   `POST /api/payment/confirm_payment`: 用户确认支付

### Admin (Requires X-Admin-Token)
*   `GET /api/admin/users`: 用户列表
*   `POST /api/admin/users/add_time`: 增加时长
*   `GET /api/admin/orders`: 订单列表
*   `POST /api/admin/orders/approve`: 审批订单
*   `GET /api/admin/config`: 获取配置
*   `POST /api/admin/config`: 更新配置

### Business
*   `POST /api/analyze/stock`: 个股 AI 分析
*   `POST /api/lhb/fetch`: 触发 LHB 抓取
*   `GET /api/stock/kline`: 获取 K 线数据
*   `GET /api/news_history/clear`: 清理新闻历史

## 5. 部署前自检清单
- [ ] `backend/data` 目录写入权限检查
- [ ] `admin_token.txt` 是否妥善保管
- [ ] 邮件 SMTP 配置是否正确 (测试发信)
- [ ] 确保服务器时间同步 (NTP)
