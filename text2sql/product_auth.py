"""Small in-memory login service for the local product UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import os
import secrets
from threading import Lock

from .product_models import (
    BUSINESS_ROLE_LABELS,
    BusinessRole,
    OrganizationAccess,
    UserContext,
    UserRole,
)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str
    display_name: str
    role: UserRole
    business_role: BusinessRole
    organization_access: OrganizationAccess
    allowed_organizations: tuple[str, ...] = ()

    def to_context(self) -> UserContext:
        return UserContext(
            user_id=self.user_id,
            role=self.role,
            allowed_organizations=self.allowed_organizations,
            business_role=self.business_role,
            organization_access=self.organization_access,
        )

    def to_dict(self) -> dict[str, object]:
        context = self.to_context()
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "business_role": self.business_role,
            "business_role_label": BUSINESS_ROLE_LABELS[self.business_role],
            "organization_access": self.organization_access,
            "allowed_organizations": list(self.allowed_organizations),
            "capabilities": {
                "can_query_data": context.can_query_data,
                "can_view_admin": context.can_view_admin,
            },
        }


class ProductAuthService:
    def __init__(self) -> None:
        self._tokens: dict[str, tuple[AuthenticatedUser, datetime]] = {}
        self._lock = Lock()

    def login(self, username: str, password: str) -> tuple[str, AuthenticatedUser]:
        normalized = username.strip()
        account = next(
            (
                (candidate, expected_password, user)
                for candidate, expected_password, user in self._accounts()
                if hmac.compare_digest(normalized, candidate)
                and hmac.compare_digest(password, expected_password)
            ),
            None,
        )
        if account is None:
            raise ValueError("账号或密码错误")

        user = account[2]
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            hours=max(1, int(os.getenv("TEXT2SQL_AUTH_TOKEN_HOURS", "12")))
        )
        with self._lock:
            self._tokens[token] = (user, expires_at)
        return token, user

    def resolve_bearer(self, authorization: str | None) -> AuthenticatedUser:
        token = self._bearer_token(authorization)
        with self._lock:
            session = self._tokens.get(token)
            if session is None:
                raise PermissionError("登录状态已失效，请重新登录")
            user, expires_at = session
            if expires_at <= datetime.now(timezone.utc):
                self._tokens.pop(token, None)
                raise PermissionError("登录状态已过期，请重新登录")
        return user

    def logout(self, authorization: str | None) -> None:
        token = self._bearer_token(authorization)
        with self._lock:
            self._tokens.pop(token, None)

    @staticmethod
    def _bearer_token(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise PermissionError("请先登录")
        token = authorization[7:].strip()
        if not token:
            raise PermissionError("请先登录")
        return token

    @staticmethod
    def _accounts() -> tuple[tuple[str, str, AuthenticatedUser], ...]:
        analyst_username = os.getenv("TEXT2SQL_ANALYST_USERNAME", "analyst")
        analyst_2_username = os.getenv("TEXT2SQL_ANALYST_2_USERNAME", "analyst2")
        analyst_3_username = os.getenv("TEXT2SQL_ANALYST_3_USERNAME", "analyst3")
        risk_username = os.getenv("TEXT2SQL_RISK_USERNAME", "risk")
        finance_username = os.getenv("TEXT2SQL_FINANCE_USERNAME", "finance")
        admin_username = os.getenv("TEXT2SQL_ADMIN_USERNAME", "admin")
        analyst_2_orgs = ProductAuthService._organizations_from_env(
            "TEXT2SQL_ANALYST_2_ORGS", "ORG001"
        )
        analyst_3_orgs = ProductAuthService._organizations_from_env(
            "TEXT2SQL_ANALYST_3_ORGS", "ORG001"
        )
        return (
            (
                analyst_username,
                os.getenv("TEXT2SQL_ANALYST_PASSWORD", "analyst123"),
                AuthenticatedUser(
                    user_id=analyst_username,
                    username=analyst_username,
                    display_name=os.getenv("TEXT2SQL_ANALYST_NAME", "业务分析员"),
                    role="analyst",
                    business_role="head_office_manager",
                    organization_access="all",
                ),
            ),
            (
                analyst_2_username,
                os.getenv("TEXT2SQL_ANALYST_2_PASSWORD", "analyst2123"),
                AuthenticatedUser(
                    user_id=analyst_2_username,
                    username=analyst_2_username,
                    display_name=os.getenv(
                        "TEXT2SQL_ANALYST_2_NAME", "分支行管理层"
                    ),
                    role="analyst",
                    business_role="branch_manager",
                    organization_access="restricted",
                    allowed_organizations=analyst_2_orgs,
                ),
            ),
            (
                analyst_3_username,
                os.getenv("TEXT2SQL_ANALYST_3_PASSWORD", "analyst3123"),
                AuthenticatedUser(
                    user_id=analyst_3_username,
                    username=analyst_3_username,
                    display_name=os.getenv(
                        "TEXT2SQL_ANALYST_3_NAME", "业务条线人员"
                    ),
                    role="analyst",
                    business_role="business_staff",
                    organization_access="restricted",
                    allowed_organizations=analyst_3_orgs,
                ),
            ),
            (
                risk_username,
                os.getenv("TEXT2SQL_RISK_PASSWORD", "risk123"),
                AuthenticatedUser(
                    user_id=risk_username,
                    username=risk_username,
                    display_name=os.getenv("TEXT2SQL_RISK_NAME", "风险合规人员"),
                    role="analyst",
                    business_role="risk_compliance",
                    organization_access="all",
                ),
            ),
            (
                finance_username,
                os.getenv("TEXT2SQL_FINANCE_PASSWORD", "finance123"),
                AuthenticatedUser(
                    user_id=finance_username,
                    username=finance_username,
                    display_name=os.getenv("TEXT2SQL_FINANCE_NAME", "财务人员"),
                    role="analyst",
                    business_role="finance_staff",
                    organization_access="all",
                ),
            ),
            (
                admin_username,
                os.getenv("TEXT2SQL_ADMIN_PASSWORD", "admin123"),
                AuthenticatedUser(
                    user_id=admin_username,
                    username=admin_username,
                    display_name=os.getenv("TEXT2SQL_ADMIN_NAME", "系统管理员"),
                    role="admin",
                    business_role="system_admin",
                    organization_access="none",
                ),
            ),
        )

    @staticmethod
    def _organizations_from_env(name: str, default: str) -> tuple[str, ...]:
        return tuple(
            value.strip()
            for value in os.getenv(name, default).split(",")
            if value.strip()
        )
