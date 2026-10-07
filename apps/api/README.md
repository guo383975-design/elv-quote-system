# 智能化弱电报价系统 API

FastAPI + SQLAlchemy 2 (async) + PostgreSQL 16 后端服务。
本目录是 monorepo 的 `apps/api` 应用。

## 快速开始

```bash
cd apps/api

python -m venv .venv && . .venv/Scripts/activate   # Windows
# python -m venv .venv && source .venv/bin/activate  # macOS / Linux

pip install -e ".[dev]"

cp .env.example .env        # 修改 DATABASE_URL / JWT_SECRET

# 准备 PostgreSQL 16 并建库
createdb quote_db          # 或 CREATE DATABASE quote_db;

alembic upgrade head       # 建表 + 建枚举类型
python -m scripts.seed     # 灌入宁波信息价 / 定额子目 / 设备库 / 演示项目
                           # 追加 --reset 只清业务数据再灌

# 可选：导入 2018 版第五册真值定额子目（843 条：编号 / 名称 / 章节）
# 只导编号与名称，人工消耗量与基价仍是空的 —— 用来替换 seed 的占位编号
python -m scripts.import_quota_zj2018 --dry-run   # 先看统计
python -m scripts.import_quota_zj2018             # 导入 + 退役占位示意值

uvicorn app.main:app --reload --port 8000
```

启动后：

| 地址 | 用途 |
|---|---|
| http://127.0.0.1:8000/api/docs | Swagger UI |
| http://127.0.0.1:8000/api/openapi.json | OpenAPI schema |
| http://127.0.0.1:8000/api/health | 健康检查 |

### 测试

集成测试跑在**内存 SQLite**（`aiosqlite` + `StaticPool`）上，不需要 PostgreSQL，
也不会碰 `DATABASE_URL`：

```bash
pytest -q                          # 全部
pytest tests/test_quote_compute.py -q   # 只跑费用瀑布（纯函数、手算断言）
```

> ORM 的列类型全部走 `app/db/types.py`（`UUIDType` / `JSONType` / `sa_enum`），
> 在 PostgreSQL 上用原生 `uuid` / `jsonb` / 原生枚举，在其他方言上降级为
> CHAR(32) / JSON / VARCHAR，因此同一套模型能同时支撑生产与 SQLite 测试。

### 种子账号

⚠ 演示口令，**只应出现在本机/演示环境**。2026-09-30 起：`scripts/seed.py` 在
`APP_ENV=production` 下**默认不创建**这些账号（需要时显式 `--demo-users`）；
自助注册默认关闭，账号由管理员 `POST /api/v1/users` 创建；登录页的提示也只在
开发构建里显示。

| 用户名 | 密码 | 角色 |
|---|---|---|
| admin | admin12345 | 管理员（superuser） |
| sales01 | sales12345 | 销售 |
| est01 | est12345 | 造价员 |
| fin01 | fin12345 | 财务 |
| view01 | view12345 | 只读 |

## 目录结构

```
apps/api/
├── app/
│   ├── core/
│   │   ├── config.py        # pydantic-settings，全部字段有默认值（无 .env 也能 import）
│   │   ├── security.py      # bcrypt 哈希 + JWT 签发/校验
│   │   ├── permissions.py   # RBAC 权限矩阵（角色 × 资源 × 动作）
│   │   └── logging.py       # loguru + stdlib 桥接
│   ├── db/
│   │   ├── types.py         # UUIDType / JSONType / sa_enum / drop_none_for_not_null
│   │   ├── base.py          # Base + UUID 主键 + 时间戳 mixin
│   │   └── session.py       # engine / session factory / get_db 依赖
│   ├── models/              # 10 张表 ORM
│   ├── schemas/             # Pydantic DTO
│   ├── services/
│   │   ├── quote_compute.py # 费用瀑布（双行并列：点位单价口径 + 定额口径）
│   │   ├── device_match.py  # 型号精确/模糊匹配 + 停产替代推荐
│   │   └── excel_io.py      # Excel 表头识别 + 双场景导入解析
│   ├── api/
│   │   ├── deps.py          # JWT 鉴权 + 权限依赖
│   │   └── v1/              # auth / users / projects / subsystems / devices
│   │                        # / quotes / labor / imports
│   └── main.py
├── alembic/versions/0001_initial.py
├── scripts/seed.py
└── tests/
```

## 接口一览

### 认证与用户

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/login` | OAuth2 表单登录（Swagger 的 Authorize 用） |
| POST | `/api/v1/auth/login/json` | JSON 登录（前端用） |
| POST | `/api/v1/auth/register` | 注册；**首个用户自动成为 admin + superuser** |
| GET | `/api/v1/auth/me` | 当前用户 |
| GET | `/api/v1/auth/me/permissions` | 当前用户可见资源列表（前端菜单用） |
| GET/POST/PATCH/DELETE | `/api/v1/users` | 用户管理（仅 admin） |

### 项目与子系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects` | 分页列表，支持 `keyword` / `status` / `project_type` |
| POST | `/api/v1/projects` | 创建（编号重复返回 409） |
| GET/PATCH/DELETE | `/api/v1/projects/{id}` | 详情 / 更新 / 删除（级联子系统与报价单） |
| GET | `/api/v1/projects/{id}/subsystems` | 某项目的子系统 |
| GET/POST | `/api/v1/subsystems` | 列表 / 新增（同项目同类型重复返回 409） |
| GET/PATCH/DELETE | `/api/v1/subsystems/{id}` | 删除**只解除明细行归属，不删明细行** |

### 设备库

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/devices` | 分页 + `keyword` / `category` / `status` / `brand` / `only_common` |
| GET | `/api/v1/devices/stats` | 状态/分类计数、已配替代型号数 |
| POST | `/api/v1/devices` | 新增 |
| POST | `/api/v1/devices/auto-code` | 按分类生成下一个编号（如 `V1010`） |
| GET/PATCH | `/api/v1/devices/{id}` | 详情 / 更新（标记停产、挂替代型号） |
| DELETE | `/api/v1/devices/{id}` | 停用（软删除 `is_active=False`） |
| POST | `/api/v1/devices/match` | **型号精确/模糊匹配**（场景二核心），返回候选与停产标记 |
| GET | `/api/v1/devices/{id}/replacements` | 替代型号建议：优先显式配置，其次同分类相似度 |

### 报价单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/quotes` | 分页列表（**不含**成本/毛利字段） |
| POST | `/api/v1/quotes` | 创建；`code`/`title` 可省略，省略时按项目编号自动生成 `<项目编号>-Q01`，标题取项目名 |
| GET/PATCH/DELETE | `/api/v1/quotes/{id}` | 详情（含明细）/ 改费率状态 / 删除 |
| POST | `/api/v1/quotes/{id}/compute` | **重算费用瀑布**；body 可覆盖 `tax_method`/`tax_rate`/`fee_base`/`auxiliary_base`/`auxiliary_rate`/`design_rate`/`warranty_rate`/`training_fee`/`mgmt_profit_rate`/`labor_float_rate`/`labor_float_per_class`/`notes`，返回体含 `warnings`（如「定额库未加载」或「缺某类工日信息价」） |
| GET | `/api/v1/quotes/{id}/subsystems` | 客户版子系统汇总（`share` 已是百分数，不要再 ×100） |
| GET/POST | `/api/v1/quotes/{id}/items` | 明细行列表 / 添加（可来自设备库或手工） |
| PATCH/DELETE | `/api/v1/quotes/{id}/items/{item_id}` | 改名称/型号/单位/子系统/数量/单价 / 删行 |
| POST | `/api/v1/quotes/{id}/items/{item_id}/replace` | 停产型号换替代（替代型号自身停产则 400） |
| GET | `/api/v1/quotes/{id}/cost` | 对内成本与毛利（仅 admin/estimator/finance） |
| GET | `/api/v1/quotes/{id}/audit-logs` | 操作留痕（仅 admin/finance） |
| GET | `/api/v1/quotes/{id}/export.xlsx` | **导出 Excel**（客户版）；`?internal=true` 出内部版 |
| GET | `/api/v1/quotes/{id}/export.pdf` | **导出 PDF**（客户版）；`?internal=true` 出内部版 |

### 导出 Excel / PDF

两个端点都要求 `quote.export`，默认给**客户版**（不含任何成本字段）。

`internal=true` 是**另一条权限线**：要求 `quote.export` **且**能看成本
（`admin`/`estimator`/`finance`）。两者不是同一个集合 —— `sales` **能导出但不能看成本**，
它要内部版会得到 **403**，而不是「悄悄给一份没有成本列的客户版」
（后者会让人以为成本是 0，比报错更坑）。

| 角色 | 客户版 | 内部版 |
|---|---|---|
| admin / finance | ✅ | ✅ |
| sales | ✅ | **403** |
| estimator | ✅（2026-09-29 起 `quote.EXPORT` 含 estimator） | ✅ |
| viewer | 403 | 403 |

- Excel 三张表：`报价汇总` / `报价明细` / `子系统汇总`。金额写成**数字**（可求和）
  并套 `#,##0.00`；已设 A4、横向、适应页宽、跨页重复表头、冻结表头。
- PDF 用 `reportlab` + **系统里的中文字体**渲染（A4、明细跨页重复表头、
  页脚「第 X 页 / 共 Y 页」）。**必须装中文字体**，否则中文是黑方块：
  `apt-get install -y fonts-wqy-zenhei`（`deploy/deploy.sh` 第 1 步已包含并会验证）。
  可用 `QUOTE_EXPORT_FONT` 指定别的字体文件，支持 `/path/x.ttc#0` 语法挑子字体。
  找不到字体时接口返回 **503 并说明缺什么**，不会静默退化成 Helvetica。
- 取值与判定统一在 `services/export_data.py`（渲染器只认这个结构），
  所以不会出现「Excel 有某列、PDF 没有」的偏差。空串表示**该口径没有这一项**，
  渲染成空单元格而**不是 0**。
- 每次导出写一条 `action=export` 的审计留痕；写失败不影响下载。

### 人工信息价与定额

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/labor-rates` | 列表（可 `only_current`） |
| GET | `/api/v1/labor-rates/current` | 当前生效价（未录入 404）；另回 `effective_dates`（各类工日各自期次）与 `stale_warning`（期次不一致或缺某一类时非空） |
| POST | `/api/v1/labor-rates` | 录入新一期；`is_current` 是**按地区+工种**翻转的，只把同类工日的旧记录置 False（见下方说明） |
| POST | `/api/v1/labor-rates/adjust-simulate` | **调价测算**：给定期次对比人工费差额（`delta_pct` 已是百分数） |
| GET/POST | `/api/v1/quota-items` | 定额子目（含 2022.7.13 纪要工艺系数） |
| GET | `/api/v1/quota-items/{code}` | 按定额编号查 |

> **`is_current` 的语义是「该地区该工种当前生效」**，不是「该地区当前期次」。
> 一个期次要分三次提交（一/二/三类），如果每次录入都把整个地区的 `is_current` 清掉，
> 最后只剩最后提交的那一类是 current，另外两类在算定额口径时被静默跳过
> （`_compute_quota_total` 里缺价就 `continue`），定额合计会变成 0。
> `/labor-rates/current` 的 `stale_warning` 就是为这种情况准备的提示。
> 另外 `_recompute` 现在要求三类工日的当期价齐全才走定额口径，缺任何一类就退化为
> 安装费口径并在 `warnings` 里说明，**不会**给出一个看着像样的错数字。

### Excel 批量导入（双场景）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/imports/device-template` | 场景一模板列定义（`columns` 是**表头字符串数组**，不是 `{field, header}` 对象） |
| POST | `/api/v1/imports/devices/preview` | **场景一预览**：解析 + 校验，不落库；`duplicate_policy` = skip/overwrite/new；返回 `unmapped`（没认出来的表头）供前端提示 |
| POST | `/api/v1/imports/devices/commit` | 确认导入 |
| GET | `/api/v1/imports/inquiry-template` | 场景二模板列定义 |
| POST | `/api/v1/imports/inquiry/preview` | **场景二预览**：逐行 `match_device`；每行附 `level`（ok/warn/error）与 `issues`（人类可读原因），数量在**顶层** `quantity`（`query` 只是查询条件回显） |
| POST | `/api/v1/imports/inquiry/commit` | 把确认的匹配行写入报价单；`multipart/form-data`，字段 `quote_id` + `payload`（JSON 数组）；未匹配行以 0 价占位；停产型号自动换替代 |

### 元数据

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/meta/enums` | 全部枚举（前端下拉） |
| GET | `/api/v1/meta/labels` | 中文标签（停产/临近停产等） |

## RBAC 角色

| 角色 | 权限 |
|---|---|
| admin | 全部（代码里 `admin` 恒为 True） |
| sales | 项目 + 报价单读写 |
| estimator | 设备库 + 人工费 + 定额 + 成本视图 |
| finance | 只读 + 对内成本视图 |
| viewer | 只读客户版（拿不到成本） |

矩阵定义见 `app/core/permissions.py` 的 `PERMISSION_MATRIX`。

## 费用瀑布（`services/quote_compute.py`）

```
equipment_total   = Σ 数量 × 设备单价
install_total     = Σ 数量 × 安装单价
auxiliary_total   = 辅材基数(三选一) × auxiliary_rate
fee_base_amount   = 附加费基数(三选一)
design_fee        = fee_base_amount × design_rate
warranty_fee      = equipment_total × warranty_rate      # 维保永远按设备费
subtotal_before_tax = 以上六项之和
mgmt_profit       = subtotal_before_tax × mgmt_profit_rate
tax_amount        = (subtotal_before_tax + mgmt_profit) × tax_rate
total_with_tax    = subtotal_before_tax + mgmt_profit + tax_amount
cost_total        = Σ 数量 × 成本价（缺失时按设备单价 × 75% 估算）
gross_profit      = total_with_tax − cost_total
```

同时并行算一套**定额计价口径** `quota_total` / `quota_total_with_tax`：
按明细行 `notes` 里的 `quota:<定额编号>` 关联定额子目，用宁波人工信息价 ×
人工消耗量 × 工艺系数累加。定额库为空时退化为安装费口径并写入 `warnings`。

金额统一 `Decimal` 保留 2 位（`ROUND_HALF_UP`），比率保留 4 位。

## 已知边界

- **定额人工消耗量是示意值**：`scripts/seed.py` 里的 `人工消耗量` 仅为结构占位，
  以纸质《浙江省通用安装工程预算定额（2018）第五册》为准；界面已有
  「⚠ 定额编号待造价员实际核对」提示。
- **当期（2026）宁波人工信息价数字待确认**：代码内置 2025-09 期
  一类 150 / 二类 163 / 三类 186 元/工日，可在 `/labor-rates` 录入新期覆盖。
- ~~导出 PDF/Excel、Vue3 前端、`docker-compose` 尚未在本轮交付范围内。~~
  → **已交付**（2026-09-29/30 更正）：导出见上文「导出」一节，前端在 `apps/web`，
  `docker-compose.yml` 可起开发用 PostgreSQL。更完整的当前状态与遗留项见仓库根
  `README.md` 的「2026-09-30 审计与修复」与 `AUDIT_REPORT_2026-09-30.md`。
