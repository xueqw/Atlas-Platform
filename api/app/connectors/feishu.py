"""飞书连接器：OAuth 2.0 授权码流程 + 基础能力（拿用户信息、发消息）。

链路：login(拼授权URL) → 用户在飞书授权 → callback(code换token,存库) → 之后用 token 调飞书 API。
凭证从 settings 读（.env），绝不硬编码、绝不传给模型。
"""
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

# 先要最小权限：拿用户身份。发消息能力后续再加 im:message。
SCOPES = "contact:user.base:readonly"


def _config() -> dict:
    """读公司统一配置（管理员在界面配的 app_id/app_secret），回退 .env。"""
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
    """用授权码换 user_access_token。"""
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
    """以应用身份拿 tenant_access_token（发消息用，无需用户 OAuth）。"""
    cfg = _config()
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.post(TENANT_TOKEN_URL, json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]})
        r.raise_for_status()
        data = r.json()
        if data.get("code", -1) != 0:
            raise RuntimeError(f"飞书应用凭证无效 {data.get('code')}：{data.get('msg')}")
        return data["tenant_access_token"]


async def lookup_open_id_by_mobile(mobile: str) -> str | None:
    """用手机号查 open_id（需权限 contact:user.id:readonly）。查不到返回 None。"""
    token = await get_tenant_access_token()
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.post(BATCH_GET_ID_URL + "?user_id_type=open_id",
                              headers={"Authorization": f"Bearer {token}"}, json={"mobiles": [mobile]})
    data = r.json()
    if data.get("code", -1) != 0:
        raise RuntimeError(f"飞书查用户失败 {data.get('code')}：{data.get('msg')}")
    for user in data.get("data", {}).get("user_list", []):
        if user.get("user_id"):  # user_id_type=open_id 时此字段即 open_id
            return user["user_id"]
    return None


async def send_message(receive_id: str, text: str, receive_id_type: str = "open_id") -> dict:
    """以机器人身份发文本消息。receive_id_type 可为 open_id（默认）或 email。
    失败时把飞书的错误码+原因带出来。"""
    token = await get_tenant_access_token()
    payload = {"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        r = await client.post(f"{MESSAGE_URL}?receive_id_type={receive_id_type}",
                              headers={"Authorization": f"Bearer {token}"}, json=payload)
    data = r.json()
    if data.get("code", -1) != 0:  # 飞书业务错误码，0 才是成功
        raise RuntimeError(f"飞书错误 {data.get('code')}：{data.get('msg')}")
    return data


async def get_status(db: Session) -> dict:
    """公司统一：configured=已配应用凭证；connected=凭证有效(能拿 tenant_token)。
    不再依赖某个用户去 OAuth——机器人以应用身份给全公司成员发消息。"""
    status = {
        "provider": PROVIDER,
        "name": "飞书",
        "description": "公司统一接入：管理员配置应用凭证后，智能体即可给本公司成员发飞书消息（邮箱 / 手机号）。",
        "configured": is_configured(),
        "connected": False,
        "account_name": "",
        "actions": ["发送飞书消息"],
    }
    if not is_configured():
        return status
    try:
        await get_tenant_access_token()  # 凭证有效才算连上
        status["connected"] = True
        row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == PROVIDER))
        status["account_name"] = "应用已就绪" + (f" · 默认收件人 {row.account_name}" if row else "")
    except Exception as exc:
        status["account_name"] = f"凭证无效：{exc}"[:80]
    return status
