# Docker 生产部署

生产使用基础文件与覆盖文件共同启动：

```sh
docker compose -f compose.yaml -f compose.production.yaml up -d --build --wait
```

容器名固定为 `distribute-postgres`、`distribute-redis`、`distribute-api`、`distribute-worker`、`distribute-web`；数据卷使用 `distribute` 项目前缀。不要加载 `compose.dev.yaml`，也不要使用 `down -v` 删除业务数据。

## 私有配置

在服务器目录运行 `python3 scripts/setup.py` 创建新的 `.env`，设置独立初始管理员密码；如需随机密码，可使用 `--username admin --generate-password`。生成后将文件权限设为 `600`，仅限服务器管理员访问。此文件、业务数据、备份、证书私钥和原始账号材料均被 Git 排除。

生产 `.env` 还需设置：

```dotenv
COMPOSE_PROJECT_NAME=distribute
CORS_ORIGINS=https://distribute.example.com
COOKIE_SECURE=true
PROXY_NETWORK=reverse-proxy
```

用实际 HTTPS 域名替换示例；`PROXY_NETWORK` 指向已有入口反向代理的 Docker 网络。只有 Web 接入该网络，API、数据库和 Redis 使用本项目默认网络。初始管理员登录确认后，清空 `BOOTSTRAP_USERNAME`、`BOOTSTRAP_PASSWORD`，重建 API 和 Worker 容器；已有管理员不会因此删除。

## HTTPS 入口

入口反向代理将该域名的请求转发到 `http://distribute-web:80`，保留 `Host`，设置 `X-Forwarded-Proto: https`，并用真实客户端地址覆盖 `X-Forwarded-For`。限制请求体为至少 11 MB。不要将 API、PostgreSQL 或 Redis 端口暴露到公网。覆盖配置允许 API 信任内部代理头，仅适用于上述隔离网络和端口约束。

证书应自动续期；续期后验证并平滑加载入口代理配置。若服务器已有其他站点，先备份代理配置，只新增本域名的 server 配置；配置检查成功后再 reload。

## 发布和验证

发布目录仅包含 Git 已跟踪文件。服务器凭据和 `.env` 不进入仓库，不放在容器构建参数中，不在日志中输出。数据库和加密密钥分别备份；迁移现有业务数据时必须保留原加密密钥，并停用旧环境的 Worker，避免两个环境同时执行分发任务。

```sh
docker compose -f compose.yaml -f compose.production.yaml ps
curl --fail https://distribute.example.com/api/health
```

检查首页、登录、CSRF 和后台任务运行。容器日志已限制为每容器最多 3 个 10 MB 文件。普通更新保留 `.env` 和数据卷，再次运行启动命令即可执行必要的数据库迁移。
