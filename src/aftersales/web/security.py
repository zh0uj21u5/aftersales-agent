"""JWT identity and explicit role checks for the production gateway."""

import re
from dataclasses import dataclass, field
from typing import Literal, cast

import jwt
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm

Role = Literal["buyer", "supervisor", "admin"]
_ROLES = {"buyer", "supervisor", "admin"}


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: Role

    def require(self, *roles: Role) -> None:
        if self.role not in roles:
            raise HTTPException(403, "当前角色无权执行此操作")


@dataclass(frozen=True)
class JwtSettings:
    algorithm: Literal["HS256", "RS256"]
    key: str = field(repr=False)
    issuer: str
    audience: str


class JwtAuthenticator:
    def __init__(self, settings: JwtSettings) -> None:
        self.settings = settings
        if not settings.issuer or not settings.audience:
            raise ValueError("必须配置 JWT issuer 与 audience")
        if settings.algorithm == "HS256" and len(settings.key.encode()) >= 32:
            self.key = settings.key
        elif settings.algorithm == "RS256":
            if "PRIVATE KEY" in settings.key:
                raise ValueError("RS256 验证只接受公钥")
            self.key = RSAAlgorithm(RSAAlgorithm.SHA256).prepare_key(settings.key)
        else:
            raise ValueError("JWT 需使用至少 32 字节的 HS256 密钥或 RS256 公钥")

    def authenticate(self, authorization: str) -> Principal:
        error = HTTPException(401, "请提供有效的登录令牌", headers={"WWW-Authenticate": "Bearer"})
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 8192:
            raise error
        try:
            claims = jwt.decode(
                token,
                self.key,
                algorithms=[self.settings.algorithm],
                issuer=self.settings.issuer,
                audience=self.settings.audience,
                options={
                    "require": ["exp", "iss", "aud", "sub", "role"],
                },
            )
        except (jwt.InvalidTokenError, ValueError, TypeError):
            raise error from None
        subject, role = claims["sub"], claims["role"]
        if (
            not isinstance(subject, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", subject)
            or not isinstance(role, str)
            or role not in _ROLES
        ):
            raise error
        return Principal(subject, cast(Role, role))
