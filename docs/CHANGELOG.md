# 版本更新记录

## v3.1.0 (2026-03-12)

- 新增认证 API 前缀后台配置（`/api/admin/auth_api_prefix`），并支持环境变量 `AUTH_API_PREFIX` 覆盖。
- 服务端托管的前端页面（`/`、`/index.html`、`/lhb.html`、`/help.html`、管理后台入口）改为按请求动态注入认证前缀，修改后无需重新部署页面即可生效。
- 前端打包脚本 `scripts/package_frontend.py` 新增认证前缀来源二选一：`manual`（手动路径）/ `env`（环境变量），兼容原有打包方式。
- `Server-Version/update.sh` 更新时保留已存在的 `DISABLE_PUBLIC_FRONTEND`、`AUTH_API_PREFIX`、`STATUS_RATE_LIMIT_WINDOW_SECONDS`、`STATUS_RATE_LIMIT_MAX_REQUESTS` 环境变量。
- 新增一键删除所有游客账户能力，并在管理列表与安全日志中增加 IP 国家/城市展示。
- 版本升级为 `v3.1.0`，并同步更新 Minimal 最小部署包。

## v3.0.2 (2026-03-05)

### 后端
- 统一服务器版本号为 `v3.0.2`（`backend/app/main.py`、`backend/app/api/auth.py`）。
- 增强市场情绪缓存策略：
  - 新增市场情绪落盘缓存文件 `backend/data/market_sentiment_cache.json`。
  - 启动阶段优先加载历史缓存，减少冷启动空窗。
  - 在缓存缺失时支持非交易时段单次探测抓取，补齐指数与情绪快照。

### 前端
- 统一前端版本号为 `v3.0.2`（页面标题与 `frontendVersion`）。
- 优化“等待数据”判定逻辑：仅在成交额与涨跌分布均缺失时显示等待，避免误判。

### 部署与最小包
- 同步更新 `Minimal-Server-Deploy` 对应后端与前端版本号到 `v3.0.2`。
- 更新 `Minimal-Server-Deploy/README_MINIMAL.md` 版本说明到 `v3.0.2`。
