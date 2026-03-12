# 认证 API 前缀与 `env` 模式说明

## 1. `env` 模式是什么

`env` 模式就是：打包前端时，不从命令行手动写死认证 API 前缀，而是先读取一个环境变量。

当前默认读取的环境变量名是：

```text
AUTH_API_PREFIX
```

对应打包参数：

```text
--auth-api-prefix-source env
--auth-api-prefix-env-key AUTH_API_PREFIX
```

如果读取到了环境变量，打包出来的前端会直接写入这个前缀。

例如：

```text
AUTH_API_PREFIX=/api/auth_abcd1234
```

那么打包后的前端登录接口就会走：

```text
/api/auth_abcd1234/login
/api/auth_abcd1234/login_user
/api/auth_abcd1234/register
```

## 2. 什么时候用 `env`

适合这些场景：

- 你部署前端的平台支持环境变量，例如 Cloudflare Pages。
- 你不想每次手动改命令里的认证前缀。
- 你希望测试环境、正式环境共用一套打包命令，只改环境变量值。

如果你的部署环境不方便设置环境变量，继续用 `manual` 即可。

## 3. 两种模式的区别

### `manual`

手动写死认证前缀：

```powershell
python scripts/package_frontend.py `
  --api-base https://your-api.example.com `
  --admin-path admin_xxx `
  --admin-api-prefix /api/admin_xxx `
  --auth-api-prefix-source manual `
  --auth-api-prefix /api/auth_xxx
```

### `env`

先设置环境变量，再打包：

```powershell
$env:AUTH_API_PREFIX="/api/auth_xxx"

python scripts/package_frontend.py `
  --api-base https://your-api.example.com `
  --admin-path admin_xxx `
  --admin-api-prefix /api/admin_xxx `
  --auth-api-prefix-source env `
  --auth-api-prefix-env-key AUTH_API_PREFIX
```

## 4. 在 Windows PowerShell 里怎么设置

只对当前终端窗口生效：

```powershell
$env:AUTH_API_PREFIX="/api/auth_xxx"
```

查看是否设置成功：

```powershell
echo $env:AUTH_API_PREFIX
```

清除当前窗口里的值：

```powershell
Remove-Item Env:AUTH_API_PREFIX
```

## 5. 在 Cloudflare Pages 里怎么设置

如果你的前端部署在 Cloudflare Pages：

1. 打开 Cloudflare Dashboard。
2. 进入你的 Pages 项目。
3. 打开 `Settings`。
4. 进入 `Environment variables`。
5. 新增变量：

```text
Name: AUTH_API_PREFIX
Value: /api/auth_xxx
```

然后重新部署。

注意：

- 这个环境变量是给“打包过程”读取的。
- 如果 Cloudflare Pages 只是托管你已经打好的静态文件，而不是在 Pages 构建时执行 `python scripts/package_frontend.py`，那它不会自动改你现有 zip 里的内容。
- 也就是说，`env` 模式的前提是：设置环境变量的那个环境，真的会执行打包脚本。

## 6. 你当前项目里建议怎么用

### 场景 A：前端独立部署在 CF Pages

建议：

- 在 Pages 构建环境里设置 `AUTH_API_PREFIX`
- 打包时使用 `--auth-api-prefix-source env`

这样每个环境可以自己控制认证前缀。

### 场景 B：前端是你本机先打包，再上传到 CF Pages

建议：

- 在你本机 PowerShell 里先设置 `AUTH_API_PREFIX`
- 然后运行打包脚本
- 再把生成出来的前端包上传

### 场景 C：服务器自己托管前端页面

这种场景下，服务端托管的页面已经支持动态注入认证前缀。

也就是说：

- 后台里改认证前缀后
- ` / `、`/index.html`、`/lhb.html`、`/help.html`
- 以及后台入口页

都会自动跟着变，不需要重新打包这些服务端托管页面。

## 7. 后台设置和环境变量谁优先

后端运行时优先级是：

1. 环境变量 `AUTH_API_PREFIX`
2. 后台管理里保存的认证前缀
3. 默认值 `/api/auth`

所以如果你服务器上已经设置了：

```text
AUTH_API_PREFIX=/api/auth_prod_xxx
```

那么后台里改了别的值，运行时仍然会以环境变量为准。

管理后台里也会提示这一点。

## 8. profile 模式怎么配

如果你平时直接运行：

```powershell
python scripts/package_frontend.py
```

脚本会进入环境模式，并把配置保存在：

[`package_frontend.local.conf`](/E:/Privy/Limit-Up-Sniper-Commercial/package_frontend.local.conf)

你可以在这个文件里分别配置：

```json
{
  "test": {
    "auth_api_prefix_source": "env",
    "auth_api_prefix_env_key": "AUTH_API_PREFIX"
  },
  "prod": {
    "auth_api_prefix_source": "manual",
    "auth_api_prefix": "/api/auth_prod_xxx"
  }
}
```

这样测试环境和正式环境可以分别使用不同策略。

## 9. 推荐做法

推荐统一规则如下：

- 本地测试：`manual`
- 支持构建环境变量的平台：`env`
- 服务器托管页面：直接依赖后端动态注入

这样最稳定，也最不容易把前端和后端前缀配错。
