"""Shared pytest fixtures: in-memory SQLite + ASGI test client + auth headers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.permissions import Role
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_db, set_session_factory
from app.main import app
from app.models.device import Device, DeviceCategory, DeviceStatus
from app.models.labor import LaborClass, LaborRate, QuotaCategory, QuotaItem
from app.models.user import User, UserStatus

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def engine():
    """Fresh in-memory DB per test, sharing one connection via StaticPool."""
    eng = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="function")
async def db(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    # 后台任务（AI 解析等）走应用级的会话工厂 —— 注入成测试这一个，
    # 它们才能看到同一个内存库里的数据。
    set_session_factory(factory)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client with get_db overridden onto the test session.

    The override mirrors production `get_db`: commit the request's unit of
    work on success, roll back on failure. Without the commit, writes made by
    a route (e.g. soft-disabling a device) would stay pending in the session
    and be invisible to the next request.
    """

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        try:
            yield db
        except Exception:
            await db.rollback()
            raise
        else:
            await db.commit()

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_user(db: AsyncSession) -> User:
    u = User(
        username="admin",
        email="admin@test.com",
        hashed_password=hash_password("admin12345"),
        full_name="管理员",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
        is_superuser=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest_asyncio.fixture
async def sales_user(db: AsyncSession) -> User:
    u = User(
        username="sales",
        email="sales@test.com",
        hashed_password=hash_password("sales12345"),
        full_name="销售",
        role=Role.SALES,
        status=UserStatus.ACTIVE,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest_asyncio.fixture
async def viewer_user(db: AsyncSession) -> User:
    u = User(
        username="viewer",
        email="viewer@test.com",
        hashed_password=hash_password("viewer12345"),
        role=Role.VIEWER,
        status=UserStatus.ACTIVE,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest_asyncio.fixture
async def estimator_user(db: AsyncSession) -> User:
    u = User(
        username="estimator",
        email="estimator@test.com",
        hashed_password=hash_password("est12345"),
        full_name="造价员",
        role=Role.ESTIMATOR,
        status=UserStatus.ACTIVE,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest_asyncio.fixture
async def finance_user(db: AsyncSession) -> User:
    """财务：只读 + 成本可见 + audit_log.read。

    这个角色在权限上很特殊（能看审计与毛利、但什么都不能写），
    没有 fixture 的话每个用到它的测试都要自己造，容易漏。
    """
    u = User(
        username="finance",
        email="finance@test.com",
        hashed_password=hash_password("fin12345"),
        full_name="财务",
        role=Role.FINANCE,
        status=UserStatus.ACTIVE,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest_asyncio.fixture
async def manager_user(db: AsyncSession) -> User:
    """经理：只读 + 审批 + 重算，不建不改报价。审批链的另一半。"""
    u = User(
        username="manager",
        email="manager@test.com",
        hashed_password=hash_password("mgr12345"),
        full_name="经理",
        role=Role.MANAGER,
        status=UserStatus.ACTIVE,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _login(client: AsyncClient, username: str, password: str) -> dict[str, str]:
    r = await client.post(
        "/api/v1/auth/login/json", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient, admin_user: User) -> dict[str, str]:
    return await _login(client, "admin", "admin12345")


@pytest_asyncio.fixture
async def sales_headers(client: AsyncClient, sales_user: User) -> dict[str, str]:
    return await _login(client, "sales", "sales12345")


@pytest_asyncio.fixture
async def viewer_headers(client: AsyncClient, viewer_user: User) -> dict[str, str]:
    return await _login(client, "viewer", "viewer12345")


@pytest_asyncio.fixture
async def est_headers(client: AsyncClient, estimator_user: User) -> dict[str, str]:
    return await _login(client, "estimator", "est12345")


@pytest_asyncio.fixture
async def finance_headers(client: AsyncClient, finance_user: User) -> dict[str, str]:
    return await _login(client, "finance", "fin12345")


@pytest_asyncio.fixture
async def manager_headers(client: AsyncClient, manager_user: User) -> dict[str, str]:
    return await _login(client, "manager", "mgr12345")


# ---------------------------------------------------------------------------
# domain fixtures — 宁波人工信息价 + 定额子目 + 设备
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def labor_rates(db: AsyncSession) -> list[LaborRate]:
    """宁波市 2025-09 期信息价（一类 150 / 二类 163 / 三类 186）."""
    eff = date(2025, 9, 1)
    rows = [
        LaborRate(region="ningbo", labor_class=LaborClass.A, effective_date=eff,
                  price_per_day=Decimal("150.00"), is_current=True),
        LaborRate(region="ningbo", labor_class=LaborClass.B, effective_date=eff,
                  price_per_day=Decimal("163.00"), is_current=True),
        LaborRate(region="ningbo", labor_class=LaborClass.C, effective_date=eff,
                  price_per_day=Decimal("186.00"), is_current=True),
    ]
    db.add_all(rows)
    await db.commit()
    return rows


@pytest_asyncio.fixture
async def quota_items(db: AsyncSession) -> list[QuotaItem]:
    rows = [
        QuotaItem(code="5-6-001", category=QuotaCategory.SECURITY,
                  name="安装摄像机", unit="台",
                  labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
                  labor_c_day=Decimal("0.00"), base_price=Decimal("112.60")),
        QuotaItem(code="5-1-001", category=QuotaCategory.CABLING,
                  name="管内穿放双绞线", unit="100m",
                  labor_a_day=Decimal("0.42"), labor_b_day=Decimal("1.85"),
                  labor_c_day=Decimal("0.00"), base_price=Decimal("128.50")),
    ]
    db.add_all(rows)
    await db.commit()
    return rows


@pytest_asyncio.fixture
async def devices(db: AsyncSession) -> list[Device]:
    rows = [
        Device(code="V1001", name="网络高清枪型摄像机 400万", brand="海康威视",
               model="DS-2CD2T46WD-I5", unit="台", category=DeviceCategory.VIDEO,
               reference_price=Decimal("680.00"), cost_price=Decimal("510.00"),
               install_unit_price=Decimal("112.60"), status=DeviceStatus.ON_SALE),
        Device(code="V1008", name="网络高清枪型摄像机 400万", brand="大华",
               model="DH-IPC-HFW3441M", unit="台", category=DeviceCategory.VIDEO,
               reference_price=Decimal("645.00"), cost_price=Decimal("484.00"),
               install_unit_price=Decimal("112.60"), status=DeviceStatus.EOL),
        Device(code="V1009", name="网络高清枪型摄像机 400万（大华新品）", brand="大华",
               model="DH-IPC-HFW3449M", unit="台", category=DeviceCategory.VIDEO,
               reference_price=Decimal("688.00"), cost_price=Decimal("516.00"),
               install_unit_price=Decimal("112.60"), status=DeviceStatus.ON_SALE),
        Device(code="N5001", name="24 口千兆交换机", brand="华为",
               model="S1730S-L24T-A", unit="台", category=DeviceCategory.NETWORK,
               reference_price=Decimal("1380.00"), cost_price=Decimal("1035.00"),
               install_unit_price=Decimal("0.00"), status=DeviceStatus.ON_SALE,
               is_owner_supplied=True),
    ]
    db.add_all(rows)
    await db.commit()
    for r in rows:
        await db.refresh(r)
    # V1008 的替代型号指向 V1009
    rows[1].replacement_id = rows[2].id
    await db.commit()
    return rows
