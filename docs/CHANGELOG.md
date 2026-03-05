# 版本更新记录

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
