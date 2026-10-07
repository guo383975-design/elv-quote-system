"""智能化弱电报价系统 API。"""

# ★ 版本号的唯一来源：/api/health 与前端显示都用它。
#   改版本时**必须同时**改这三处（Python/Node 的打包格式不支持引用变量）：
#     apps/api/pyproject.toml  version = "..."
#     apps/web/package.json    "version": "..."
__version__ = "0.1.7"
