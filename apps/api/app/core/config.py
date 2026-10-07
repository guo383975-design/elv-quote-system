"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__

# 公开的占位密钥。**只允许在非 production 下使用**：
# 它出现在本文件里就等于公开，谁都能用它签发任意用户的令牌。
# 为什么要有这个常量：以前 jwt_secret 的默认值就是一个公开字符串，而
# production 下 .env 缺失/被截断时应用照常启动 —— 于是「缺配置」这种小事
# 会静默降级成「任何人都能伪造管理员令牌」。见 validate_for_runtime()。
DEV_JWT_SECRET = "dev-secret-change-me"

# 生产环境要求的最小密钥长度（HS256 的密钥熵下限，32 字节 = 64 hex）。
MIN_JWT_SECRET_LEN = 32


class Settings(BaseSettings):
    """All app settings live here. Loaded from `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- Database -----
    database_url: str = Field(
        default="postgresql+asyncpg://quote:quote@localhost:5432/quote_db",
        description="Async SQLAlchemy URL",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg://quote:quote@localhost:5432/quote_db",
        description="Sync URL for Alembic migrations (psycopg3; psycopg2 has no py3.14 wheels)",
    )

    # ----- JWT -----
    jwt_secret: str = Field(
        default=DEV_JWT_SECRET,
        description=(
            "HMAC secret for JWT signing。"
            "production 下必须是随机值，见 validate_for_runtime()"
        ),
    )
    jwt_algorithm: str = Field(default="HS256")
    jwt_expire_minutes: int = Field(default=1440, description="24 hours default")

    # ----- 首次引导（自助注册的唯一入口）-----
    # 默认空 = 自助注册**完全关闭**。设计口径（报价系统设计方案.md §9.1）：
    # 内网不开放自助注册，账号由管理员创建（POST /users）。
    # 全新部署需要一个「第一个用户」时：在 .env 里设一个一次性随机串，
    # 用它调一次 POST /auth/register，注册完就把这行删掉。
    bootstrap_token: str = Field(
        default="",
        description="非空时允许用它在空库上创建第一个管理员；空 = 关闭自助注册",
    )

    # ----- App -----
    app_env: str = Field(default="development")
    app_name: str = Field(default="智能化弱电报价系统 API")
    # 从 app/__init__.py 取（唯一来源），避免两处版本号各写各的
    app_version: str = Field(default=__version__)
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)

    # ----- CORS -----
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        description="Comma-separated allowed origins",
    )

    # ----- Files -----
    upload_dir: Path = Field(default=Path("./uploads"))
    max_upload_mb: int = Field(default=20)

    # ----- Logging -----
    log_level: str = Field(default="INFO")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ----- 启动前的安全检查 -----

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    def validate_for_runtime(self) -> None:
        """production 下配置不安全就拒绝启动（fail fast）。

        以前这里是静默降级：`.env` 没读到（文件缺失、被 `>` 截断、CWD 不对）
        就退回公开默认密钥，令牌人人可伪造，而服务照常 healthy。
        宁可起不来也不要「看起来正常、实际不设防」。

        调用点：`app.main` 的 lifespan（uvicorn 启动时）与 `scripts/seed.py`。
        测试用 ASGITransport、不跑 lifespan，所以不受影响。
        """
        if not self.is_production:
            return
        problems: list[str] = []
        if self.jwt_secret == DEV_JWT_SECRET:
            problems.append(
                "JWT_SECRET 仍是公开的默认值（.env 缺失或没写这一行？）"
            )
        elif len(self.jwt_secret) < MIN_JWT_SECRET_LEN:
            problems.append(
                f"JWT_SECRET 太短（{len(self.jwt_secret)} < {MIN_JWT_SECRET_LEN} 字符）"
            )
        if problems:
            raise RuntimeError(
                "APP_ENV=production 但配置不安全，已拒绝启动：\n  - "
                + "\n  - ".join(problems)
                + "\n修复：在 apps/api/.env 里设置 "
                "JWT_SECRET=$(openssl rand -hex 32)，然后重启服务。"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton — re-reading `.env` per request would be wasteful."""
    return Settings()


settings = get_settings()
