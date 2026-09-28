"""Optional Feishu connector for OAuth and messaging."""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import ConnectorConfig, ConnectorToken

PROVIDER = "feishu"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
USERINFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
TENANT_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
BATCH_GET_ID_URL = "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id"

# Request only the minimum identity scope here.
SCOPES = "contact:user.base:readonly"


def _config() -> dict:
    """Read organization settings saved by an admin, then fall back to .env."""
    with SessionLocal() as db:
        row = db.scalar(select(ConnectorConfig).where(ConnectorConfig.provider == PROVIDER))
        cfg = json.loads(row.data) if row and row.data else {}
    return {
        "app_id": cfg.get("app_id") or settings.feishu_app_id,
        "app_secret": cfg.get("app_secret") or settings.feishu_app_secret,
        "redirect_uri": cfg.get("redirect_uri") or settings.feishu_redirect_uri,
    }


def save_config(db: Session, app_id: str, app_secret: str, redirect_uri: str = "") -> None:
    row = db.scalar(select(ConnectorConfig).where(ConnectorConfig.provider == PROVIDER))
    if not row:
        row = ConnectorConfig(provider=PROVIDER)
        db.add(row)
    data = {"app_id": app_id, "app_secret": app_secret}
    if redirect_uri:
        data["redirect_uri"] = redirect_uri
    row.data = json.dumps(data)
    db.commit()


def clear_config(db: Session) -> None:
    row = db.scalar(select(ConnectorConfig).where(ConnectorConfig.provider == PROVIDER))
    if row:
        db.delete(row); db.commit()


def is_configured() -> bool:
    cfg = _config()
    return bool(cfg["app_id"] and cfg["app_secret"])


def build_authorize_url(state: str) -> str:
    cfg = _config()
    params = {
        "client_id": cfg["app_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


async def exchange_code(code: str) -> dict:
    """Exchange an authorization code for a user access token."""
    cfg = _config()
    payload = {
        "grant_type": "authorization_code",
        "client_id": cfg["app_id"],
        "client_secret": cfg["app_secret"],
        "code": code,
        "redirect_uri": cfg["redirect_uri"],
    }
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.post(TOKEN_URL, json=payload)
        r.raise_for_status()
        return r.json()


async def fetch_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json().get("data", {})


def save_token(db: Session, token_resp: dict, account_name: str, open_id: str = "") -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_resp.get("expires_in", 7200))
    row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == PROVIDER))
    if not row:
        row = ConnectorToken(provider=PROVIDER)
        db.add(row)
    row.access_token = token_resp["access_token"]
    row.refresh_token = token_resp.get("refresh_token", "")
    row.expires_at = expires_at
    row.account_name = account_name
    row.open_id = open_id
    db.commit()


async def get_tenant_access_token() -> str:
    """Get an app-level tenant token for messaging."""
    cfg = _config()
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.post(TENANT_TOKEN_URL, json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]})
        r.raise_for_status()
        data = r.json()
        if data.get("code", -1) != 0:
            raise RuntimeError(f"Invalid Feishu app credentials ({data.get('code')}): {data.get('msg')}")
        return data["tenant_access_token"]


async def lookup_open_id_by_mobile(mobile: str) -> str | None:
    """Look up a Feishu open_id by mobile number."""
    token = await get_tenant_access_token()
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.post(BATCH_GET_ID_URL + "?user_id_type=open_id",
                              headers={"Authorization": f"Bearer {token}"}, json={"mobiles": [mobile]})
    data = r.json()
    if data.get("code", -1) != 0:
        raise RuntimeError(f"Feishu user lookup failed ({data.get('code')}): {data.get('msg')}")
    for user in data.get("data", {}).get("user_list", []):
        if user.get("user_id"):  # user_id_type=open_id 时此字段即 open_id
            return user["user_id"]
    return None


async def send_message(receive_id: str, text: str, receive_id_type: str = "open_id") -> dict:
    """Send a text message as the app bot using an open_id or email address."""
    token = await get_tenant_access_token()
    payload = {"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.post(f"{MESSAGE_URL}?receive_id_type={receive_id_type}",
                              headers={"Authorization": f"Bearer {token}"}, json=payload)
    data = r.json()
    if data.get("code", -1) != 0:  # 飞书业务错误码，0 才是成功
        raise RuntimeError(f"Feishu error ({data.get('code')}): {data.get('msg')}")
    return data


async def get_status(db: Session) -> dict:
    """Return organization-level configuration and connectivity status."""
    status = {
        "provider": PROVIDER,
        "name": "Feishu (optional)",
        "description": "Optional organization connector for sending Feishu messages by email address or mobile number.",
        "configured": is_configured(),
        "connected": False,
        "account_name": "",
        "actions": ["Send Feishu messages"],
    }
    if not is_configured():
        return status
    try:
        await get_tenant_access_token()  # 凭证有效才算连上
        status["connected"] = True
        row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == PROVIDER))
        status["account_name"] = "App ready" + (f" · default recipient {row.account_name}" if row else "")
    except Exception as exc:
        status["account_name"] = f"Invalid credentials: {exc}"[:80]
    return status
