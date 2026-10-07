"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1 import (
    ai,
    audit,
    auth,
    customers,
    devices,
    exports,
    imports,
    labor,
    notifications,
    point_templates,
    projects,
    quotes,
    reports,
    settings,
    subsystems,
    suppliers,
    users,
)

api_router = APIRouter()
api_router.include_router(ai.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(projects.router)
api_router.include_router(subsystems.router)
api_router.include_router(devices.router)
api_router.include_router(quotes.router)
api_router.include_router(exports.router)
api_router.include_router(labor.router)
api_router.include_router(imports.router)
api_router.include_router(suppliers.router)
api_router.include_router(customers.router)
api_router.include_router(notifications.router)
api_router.include_router(point_templates.router)
api_router.include_router(reports.router)
api_router.include_router(audit.router)
api_router.include_router(settings.router)

__all__ = ["api_router"]




