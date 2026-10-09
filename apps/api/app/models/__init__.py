"""SQLAlchemy ORM models — re-export every model class.

Important: importing this module triggers SQLAlchemy mapper registration.
Add new model files here.
"""

from app.models.ai import (
    AiConversation,
    AiMessage,
    AiMessageRole,
    AiParseJob,
    AiParseStatus,
    AiTask,
    AiUsageLog,
)
from app.models.audit import (
    AuditAction,
    DeviceAuditAction,
    DeviceAuditEntity,
    DeviceAuditLog,
    LoginAuditLog,
    ProjectAuditLog,
    QuoteAuditLog,
)
from app.models.customer import Customer
from app.models.department import Department
from app.models.device import (
    Device,
    DeviceCategory,
    DeviceCategoryNode,
    DevicePriceTier,
    DeviceStatus,
)
from app.models.factory_reset_log import FactoryResetLog
from app.models.import_batch import ImportBatch, ImportKind, ImportStatus
from app.models.labor import LaborClass, LaborRate, QuotaCategory, QuotaItem
from app.models.notification import Notification, NotificationKind
from app.models.point_template import PointTemplate, PointTemplateItem
from app.models.project import Project, ProjectStatus, ProjectType
from app.models.quote import (
    AuxiliaryBaseType,
    FeeBaseType,
    Quote,
    QuoteStatus,
)
from app.models.quote_approval import QuoteApproval
from app.models.quote_item import ItemSource, QuoteItem
from app.models.quote_revision import QuoteRevision
from app.models.setting import AppSetting
from app.models.subsystem import Subsystem, SubsystemType
from app.models.supplier import Supplier
from app.models.user import User, UserStatus

__all__ = [
    "LoginAuditLog",
    "AppSetting",
    "Notification",
    "NotificationKind",
    "PointTemplate",
    "PointTemplateItem",
    "User",
    "UserStatus",
    "Customer",
    "Department",
    "ImportBatch",
    "ImportKind",
    "ImportStatus",
    "Project",
    "ProjectType",
    "ProjectStatus",
    "Subsystem",
    "SubsystemType",
    "Supplier",
    "Device",
    "DevicePriceTier",
    "DeviceCategory",
    "DeviceCategoryNode",
    "DeviceStatus",
    "LaborRate",
    "LaborClass",
    "QuotaItem",
    "QuotaCategory",
    "Quote",
    "QuoteStatus",
    "FeeBaseType",
    "AuxiliaryBaseType",
    "QuoteItem",
    "QuoteApproval",
    "QuoteRevision",
    "ItemSource",
    "QuoteAuditLog",
    "ProjectAuditLog",
    "DeviceAuditLog",
    "DeviceAuditEntity",
    "DeviceAuditAction",
    "AiConversation",
    "AiMessage",
    "AiMessageRole",
    "AiParseJob",
    "AiParseStatus",
    "AiTask",
    "AiUsageLog",
    "AuditAction",
    "FactoryResetLog",
]




