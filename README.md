# OpenAI Register Ops 控制面板

这是一个基于 FastAPI + APScheduler 的本机控制面板，用于账号池自动注册、失败触发刷新、状态查看与账号导出。

## 功能特性

- 按阈值自动补齐账号池（`pool_target`，默认 `20`）
- 可配置注册并发（`register_concurrency`，默认 `5`）
- 失败触发刷新流程（业务调用返回 `401/403` 时刷新并重试一次）
- 支持单个删除、批量删除与一键全删账号
- 使用 JSON 文件管理索引、配置和日志
- Web 控制面板页面（`/`、`/accounts`、`/logs`）
- Docker 单容器启动

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn web.app:app --host 127.0.0.1 --port 8000
```

## Docker 运行

```powershell
docker build -t openai-register-ops .
docker run --rm -p 127.0.0.1:8000:8000 -v ${PWD}/data:/app/data openai-register-ops
```

## 数据目录结构

- `data/accounts/`：账号 token 文件（`token_<email>_<ts>.json`）
- `data/index/accounts_index.json`：账号索引
- `data/config/runtime_config.json`：运行配置
- `data/logs/events.log`：事件日志（最多保留最近 1000 条）

## 运行配置字段

- `pool_target`：账号池目标数量
- `auto_register_enabled`：是否启用自动注册任务
- `auto_refresh_enabled`：是否启用自动刷新任务
- `register_concurrency`：注册并发数
- `scheduler_recover_on_boot`：启动时是否自动恢复任务
- `proxy`：可选代理地址

## API 列表

- `GET /api/status`
- `GET /api/accounts`
- `DELETE /api/accounts/{account_id}`
- `POST /api/accounts/delete-batch`
- `POST /api/accounts/delete-all`
- `GET /api/accounts/export`
- `POST /api/register/once`
- `POST /api/register/refill`
- `POST /api/refresh/account/{account_id}`
- `POST /api/jobs/toggle`
- `PATCH /api/config`
- `GET /api/logs?limit=200`

## 导出说明

`GET /api/accounts/export` 会下载一个 ZIP 包，包内是多份 `token_*.json` 文件：

- 文件名保持账号 token 文件风格（与 `D:\chromedownland\Compressed\oai` 目录一致）
- 每个文件是单行 JSON（minified）
- 内容为该账号完整 token 数据（如 `id_token/access_token/refresh_token/account_id/email/...`）

## 安全说明

- 当前设计按你的要求为明文存储：账号 token、导出文件均为明文。
- 面板默认用于本机访问，不建议直接暴露到公网。

## 测试

```powershell
python -m unittest discover -s tests
```
