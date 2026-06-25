"""多租户认证地基：账号密码登录 + 服务端 httpOnly cookie 会话。

入口认证用平台自有账号（启动时预置几个测试账号，登录页点选即可登录）。
飞书属于「连接器」能力（连接器页的「授权身份」），不参与入口认证。
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .config import settings
from .database import SessionLocal, get_db
from .models import Membership, Session, User, Workspace

COOKIE_NAME = "atlas_session"

# 预置测试账号：登录页直接点选登录，共用同一测试密码（settings.test_account_password）
TEST_ACCOUNTS = [
    {"username": "admin", "name": "管理员"},
    {"username": "alice", "name": "Alice 张"},
    {"username": "bob", "name": "Bob 李"},
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# === 密码哈希：stdlib pbkdf2，免重型依赖 ===

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"pbkdf2_sha256$100000${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, AttributeError):
        return False


def get_or_create_default_workspace(db: DbSession) -> Workspace:
    """全局默认工作区：承载 Phase 1 之前的历史数据，新用户都归入它。"""
    ws = db.scalar(select(Workspace).order_by(Workspace.created_at))
    if not ws:
        ws = Workspace(name="默认工作区")
        db.add(ws)
        db.flush()
    return ws


def ensure_membership(db: DbSession, user: User, workspace: Workspace) -> Membership:
    m = db.scalar(
        select(Membership).where(Membership.user_id == user.id, Membership.workspace_id == workspace.id)
    )
    if m:
        return m
    existing = db.scalar(
        select(func.count()).select_from(Membership).where(Membership.workspace_id == workspace.id)
    )
    m = Membership(user_id=user.id, workspace_id=workspace.id, role="owner" if not existing else "member")
    db.add(m)
    db.flush()
    return m


def seed_test_accounts() -> None:
    """幂等地建默认工作区 + 预置测试账号（首个=owner，其余=member）。"""
    with SessionLocal() as db:
        workspace = get_or_create_default_workspace(db)
        for spec in TEST_ACCOUNTS:
            user = db.scalar(select(User).where(User.username == spec["username"]))
            if not user:
                user = User(
                    username=spec["username"], name=spec["name"],
                    password_hash=hash_password(settings.test_account_password),
                )
                db.add(user)
                db.flush()
            ensure_membership(db, user, workspace)
        db.commit()


def authenticate(db: DbSession, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user and verify_password(password, user.password_hash):
        return user
    return None


def start_session(db: DbSession, user: User) -> str:
    """为已认证用户签发会话，落到其默认工作区。返回 session token。"""
    workspace = get_or_create_default_workspace(db)
    ensure_membership(db, user, workspace)
    token = secrets.token_urlsafe(32)
    db.add(Session(
        id=token, user_id=user.id, workspace_id=workspace.id,
        expires_at=_now() + timedelta(hours=settings.session_ttl_hours),
    ))
    db.commit()
    return token


def destroy_session(db: DbSession, token: str | None) -> None:
    if not token:
        return
    sess = db.get(Session, token)
    if sess:
        db.delete(sess)
        db.commit()


def current_session(
    atlas_session: str | None = Cookie(default=None),
    db: DbSession = Depends(get_db),
) -> Session:
    if not atlas_session:
        raise HTTPException(401, "未登录")
    sess = db.get(Session, atlas_session)
    if not sess:
        raise HTTPException(401, "会话无效，请重新登录")
    expires = sess.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < _now():
        db.delete(sess)
        db.commit()
        raise HTTPException(401, "会话已过期，请重新登录")
    return sess


def current_user(
    sess: Session = Depends(current_session),
    db: DbSession = Depends(get_db),
) -> User:
    user = db.get(User, sess.user_id)
    if not user:
        raise HTTPException(401, "用户不存在")
    return user


def current_workspace_id(sess: Session = Depends(current_session)) -> str:
    """当前会话锁定的租户边界——所有资源查询/写入都以它为作用域。"""
    return sess.workspace_id
