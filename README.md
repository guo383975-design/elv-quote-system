# 智能化弱电报价系统

面向**智能化弱电工程**（监控、门禁、网络、机房、综合布线等）的报价与项目管理系统。
从设备清单、定额取价到报价版本、导出打印，走一条可追溯的流程。

> 本仓库是**应用代码**部分：后端 API + 前端单页应用 + 数据库结构。

## 功能概览

- **报价单**：设备/材料清单录入、工程量与单价、折扣与税率、合计与毛利
- **报价版本**：每次保存可留痕为一个版本，版本标记精确到秒，可回溯
- **设备库**：设备分类、批量调价、软删除；导入不忽略「设备状态 / 供应商 / 替代型号」
- **定额库**：定额子目数据随应用分发，供取价使用
- **操作留痕**：新增 / 修改 / 软删 / 分类增删 / 批量调价均记录操作人
- **导出与打印**：Excel / PDF 导出，含公式注入防护
- **权限**：基于角色的菜单与接口权限控制
- **AI 辅助**：解析报价相关的自然语言/文档（可选，依赖外部模型服务）

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Vue 3 · TypeScript · Vite · Element Plus · Pinia · Vue Router · axios · markdown-it |
| 后端 | Python ≥3.11 · FastAPI · SQLAlchemy 2（async）· asyncpg · Alembic · Pydantic v2 |
| 数据库 | PostgreSQL 16（`docker-compose.yml` 提供 `postgres:16-alpine`） |
| 鉴权 | JWT（HS256，`python-jose`）· 口令哈希 `bcrypt`（`$2b$12$`） |

## 目录结构

```
apps/
  api/                 FastAPI 服务
    app/               应用代码（api / core / db / models / services）
    alembic/           数据库迁移
    tests/             测试
    scripts/           工具脚本与随包数据（定额库）
    pyproject.toml
  web/                 Vue 3 前端
    src/               源码（views / components / stores / utils）
    package.json
数据库设计.sql          数据库结构参考
docker-compose.yml      本地起 PostgreSQL
```

## 快速开始

### 1. 起数据库

```bash
docker compose up -d db
```

### 2. 后端

```bash
cd apps/api
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env                              # 然后按注释改 DATABASE_URL / JWT_SECRET
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> `JWT_SECRET` 是 HS256 对称密钥，**务必改成你自己的随机值**：
> `JWT_SECRET` 仍是公开的默认值时，`APP_ENV=production` 会**拒绝启动**（这是有意的安全闸门）。

### 3. 前端

```bash
cd apps/web
npm install
npm run dev          # 开发；构建用 npm run build
```

## 配置

所有环境变量见 `apps/api/.env.example`（内含注释说明）。**不要提交真实的 `.env`**。

## 测试

```bash
cd apps/api && pytest
cd apps/web && npm run typecheck
```

## 许可

见 `LICENSE`（如未随仓库提供，请在使用前与作者确认）。
