# 最小部署包

- 版本: v3.0.2
- 数据模式: 使用空白模板数据（适合新部署）

## 包含内容
- backend/app
- backend/requirements.txt
- backend/data（最小化模板）
- frontend（页面与静态资源）
- Server-Version（install/update/uninstall 脚本）

## 使用方式
1. 将本目录上传到服务器（建议 `/opt/limit-up-sniper-commercial`）。
2. 首次部署执行：`sudo bash Server-Version/install.sh`
3. 后续更新执行：`sudo bash Server-Version/update.sh`

## 冲突合并后建议验收
```bash
grep -R -nE '^(<<<<<<<|=======|>>>>>>>)' Server-Version/install.sh Server-Version/update.sh
bash -n Server-Version/install.sh
bash -n Server-Version/update.sh
```




