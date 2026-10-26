import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


JWT_ALGORITHM = "HS256"
bearer_scheme = HTTPBearer(auto_error=False)

# 历史版本的默认密钥：即使被写入环境也视为未配置（B1）。
_DEPRECATED_DEFAULT_SECRET = "change-this-development-secret"


def require_jwt_secret() -> str:
    """读取 JWT 密钥；未配置（或等于历史默认值）时直接抛错。

    绝不回退到硬编码默认密钥——该密钥公开存在于仓库历史中，
    拿到它就能签发任意用户 token，完全绕过登录（B1）。
    """
    secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if not secret or secret == _DEPRECATED_DEFAULT_SECRET:
        raise RuntimeError(
            "JWT_SECRET_KEY 未配置（或仍是历史默认值）。"
            "为防止伪造 token，系统拒绝签发/校验 JWT。"
            "请在 .env 中设置至少 32 位的随机密钥后重启。"
        )
    return secret


def assert_jwt_configured() -> None:
    """启动期自检：密钥缺失即抛错，由 lifespan 终止应用启动（B1）。"""
    require_jwt_secret()


def create_access_token(user_id: str, username: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))
    )
    payload = {"sub": user_id, "username": username, "exp": expires_at}
    return jwt.encode(payload, require_jwt_secret(), algorithm=JWT_ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            require_jwt_secret(),
            algorithms=[JWT_ALGORITHM],
        )
        user_id = payload.get("sub")
        username = payload.get("username")
        if not user_id or not username:
            raise ValueError("missing claims")
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc

    return {"id": user_id, "username": username}
