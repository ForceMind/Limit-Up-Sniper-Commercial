from fastapi import APIRouter, Depends, HTTPException, Body, Request, Header
from sqlalchemy.orm import Session
from app.db import database, models, schemas
from app.core import purchase_manager, user_service, account_store
from pydantic import BaseModel
from typing import Optional, Dict, Any, Tuple
import logging
import smtplib
import ssl
import json
import html
import threading
import time
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.core.config_manager import SYSTEM_CONFIG
from app.core.runtime_logs import add_runtime_log

logger = logging.getLogger("payment")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
ADMIN_PANEL_PATH_FILE = DATA_DIR / "admin_panel_path.json"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ORDER_NOTIFY_DELAY_SECONDS = 180


router = APIRouter()


class CreateOrderRequest(BaseModel):
    target_version: str
    duration_months: int
    x_device_id: str
    invite_code: Optional[str] = None


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _duration_key_from_months(duration_months: int) -> str:
    if duration_months == 0:
        return "3d"
    if duration_months == 3:
        return "3m"
    if duration_months == 6:
        return "6m"
    if duration_months == 12:
        return "12m"
    return "1m"


def _referral_reward_days() -> int:
    cfg = SYSTEM_CONFIG.get("referral_config", {})
    if not isinstance(cfg, dict):
        return 30
    try:
        days = int(cfg.get("reward_days", 30) or 30)
    except Exception:
        days = 30
    return max(1, min(days, 365))


def _serialize_order(order: models.PurchaseOrder) -> dict:
    if not order:
        return {}
    return {
        "id": int(order.id),
        "order_code": str(order.order_code),
        "amount": float(order.amount or 0.0),
        "target_version": str(order.target_version or ""),
        "duration_days": int(order.duration_days or 0),
        "status": str(order.status or ""),
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "can_cancel": str(order.status or "") in {"pending", "waiting_verification"},
    }


def _safe_text(value: Any, limit: int = 200) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _version_label(version: str) -> str:
    v = str(version or "").strip().lower()
    mapping = {
        "trial": "试用版",
        "basic": "初级版",
        "advanced": "高级版",
        "flagship": "旗舰版",
    }
    return mapping.get(v, version or "-")


def _status_label(status: str) -> str:
    s = str(status or "").strip().lower()
    mapping = {
        "pending": "待支付",
        "waiting_verification": "待审核",
        "completed": "已完成",
        "rejected": "已拒绝",
        "cancelled": "已取消",
    }
    return mapping.get(s, status or "-")


def _format_shanghai_time(value: Optional[datetime]) -> str:
    if value is None:
        return "-"
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _request_base_url(req: Optional[Request]) -> str:
    if req is None:
        return ""
    try:
        return f"{req.url.scheme}://{req.url.netloc}".rstrip("/")
    except Exception:
        return ""


def _fallback_base_url_from_config() -> str:
    cfg = SYSTEM_CONFIG.get("referral_config", {})
    if not isinstance(cfg, dict):
        return ""
    raw = str(cfg.get("share_base_url", "") or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return ""


def _load_admin_panel_path() -> str:
    default_path = "/admin"
    if not ADMIN_PANEL_PATH_FILE.exists():
        return default_path
    try:
        data = json.loads(ADMIN_PANEL_PATH_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_path
        raw = str(data.get("path", default_path) or "").strip()
        if not raw:
            return default_path
        if not raw.startswith("/"):
            raw = "/" + raw
        parts = [p for p in raw.split("/") if p]
        if not parts:
            return default_path
        normalized = "/" + "/".join(parts)
        if normalized.startswith("/api"):
            return default_path
        return normalized
    except Exception:
        return default_path


def _build_admin_links(order_code: str, request_base_url: str, copy_payload: str) -> Tuple[str, str]:
    base_url = (request_base_url or "").strip() or _fallback_base_url_from_config()
    admin_path = _load_admin_panel_path()
    order_qs = urllib.parse.urlencode({"view": "orders", "order_code": str(order_code or "")})
    copy_qs = urllib.parse.urlencode({
        "view": "orders",
        "order_code": str(order_code or ""),
        "copy_text": copy_payload,
    })
    if base_url:
        order_link = f"{base_url}{admin_path}?{order_qs}"
        copy_link = f"{base_url}{admin_path}?{copy_qs}"
    else:
        order_link = f"{admin_path}?{order_qs}"
        copy_link = f"{admin_path}?{copy_qs}"
    return order_link, copy_link


def _build_order_email(
    order: models.PurchaseOrder,
    event_type: str,
    client_ip: str,
    user_agent: str,
    request_base_url: str,
) -> Tuple[str, str, str]:
    device_id = str(order.user.device_id or "").strip() if order.user else ""
    username = account_store.get_username_by_device_id(device_id) or "-"

    invite_info = account_store.get_order_invite(str(order.order_code)) or {}
    if not isinstance(invite_info, dict):
        invite_info = {}

    invite_code = _safe_text(invite_info.get("invite_code", "") or "-")
    inviter_username = _safe_text(invite_info.get("inviter_username", "") or "-")
    inviter_device_id = _safe_text(invite_info.get("inviter_device_id", "") or "-")
    invite_bonus_token = _safe_text(invite_info.get("bonus_token", "") or "-")

    version_cn = _version_label(order.target_version)
    status_cn = _status_label(order.status)
    now_sh = datetime.now(tz=SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    created_sh = _format_shanghai_time(order.created_at)
    updated_sh = _format_shanghai_time(order.updated_at)

    event_type_norm = str(event_type or "").strip().lower()
    if event_type_norm == "cancelled":
        event_title = "用户取消订单（即时通知）"
        subject = f"【订单取消】{order.order_code}"
    else:
        event_title = "新订单创建（延迟 3 分钟通知）"
        subject = f"【新订单待处理】{order.order_code} · ¥{float(order.amount or 0):.2f}"

    copy_lines = [
        f"事件: {event_title}",
        f"订单号: {order.order_code}",
        f"支付口令: {order.order_code}",
        f"用户: {username}",
        f"设备ID: {device_id or '-'}",
        f"设备信息: {_safe_text(user_agent or '-', 300)}",
        f"邀请码: {invite_code}",
        f"邀请人: {inviter_username}",
        f"邀请人设备ID: {inviter_device_id}",
        f"邀请奖励口令: {invite_bonus_token}",
        f"会员版本: {version_cn}",
        f"时长: {int(order.duration_days or 0)} 天",
        f"金额: ¥{float(order.amount or 0):.2f}",
        f"状态: {status_cn}",
        f"下单时间(北京): {created_sh}",
        f"更新时间(北京): {updated_sh}",
        f"触发时间(北京): {now_sh}",
        f"来源IP: {_safe_text(client_ip or '-', 64)}",
    ]
    copy_block = "\n".join(copy_lines)
    copy_one_line = " | ".join(copy_lines)

    admin_orders_link, admin_copy_link = _build_admin_links(order.order_code, request_base_url, copy_one_line)

    plain_text = (
        f"{event_title}\n\n"
        f"订单号: {order.order_code}\n"
        f"支付口令: {order.order_code}\n"
        f"用户: {username}\n"
        f"设备ID: {device_id or '-'}\n"
        f"设备信息: {_safe_text(user_agent or '-', 300)}\n"
        f"邀请码: {invite_code}\n"
        f"邀请人: {inviter_username}\n"
        f"邀请人设备ID: {inviter_device_id}\n"
        f"邀请奖励口令: {invite_bonus_token}\n"
        f"会员版本: {version_cn}\n"
        f"时长: {int(order.duration_days or 0)} 天\n"
        f"金额: ¥{float(order.amount or 0):.2f}\n"
        f"状态: {status_cn}\n"
        f"下单时间(北京): {created_sh}\n"
        f"更新时间(北京): {updated_sh}\n"
        f"触发时间(北京): {now_sh}\n"
        f"来源IP: {_safe_text(client_ip or '-', 64)}\n\n"
        f"后台订单管理: {admin_orders_link}\n"
        f"打开并一键复制: {admin_copy_link}\n\n"
        f"复制串（单行）:\n{copy_one_line}\n\n"
        f"复制块（多行）:\n{copy_block}\n"
    )

    esc = html.escape
    html_text = f"""
<html>
  <body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2937;line-height:1.6;\">
    <h2 style=\"margin:0 0 8px;\">{esc(event_title)}</h2>
    <p style=\"margin:0 0 16px;color:#6b7280;\">触发时间（北京时间）：{esc(now_sh)}</p>

    <table cellpadding=\"6\" cellspacing=\"0\" style=\"border-collapse:collapse;border:1px solid #e5e7eb;font-size:14px;\">
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">订单号</td><td style=\"border:1px solid #e5e7eb;\"><b>{esc(str(order.order_code))}</b></td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">支付口令</td><td style=\"border:1px solid #e5e7eb;\"><b>{esc(str(order.order_code))}</b></td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">用户</td><td style=\"border:1px solid #e5e7eb;\">{esc(username)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">设备ID</td><td style=\"border:1px solid #e5e7eb;\">{esc(device_id or '-')}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">设备信息</td><td style=\"border:1px solid #e5e7eb;\">{esc(_safe_text(user_agent or '-', 300))}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">邀请码</td><td style=\"border:1px solid #e5e7eb;\">{esc(invite_code)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">邀请人</td><td style=\"border:1px solid #e5e7eb;\">{esc(inviter_username)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">邀请人设备ID</td><td style=\"border:1px solid #e5e7eb;\">{esc(inviter_device_id)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">邀请奖励口令</td><td style=\"border:1px solid #e5e7eb;\">{esc(invite_bonus_token)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">会员版本</td><td style=\"border:1px solid #e5e7eb;\">{esc(version_cn)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">时长</td><td style=\"border:1px solid #e5e7eb;\">{int(order.duration_days or 0)} 天</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">金额</td><td style=\"border:1px solid #e5e7eb;\">¥{float(order.amount or 0):.2f}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">状态</td><td style=\"border:1px solid #e5e7eb;\">{esc(status_cn)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">下单时间(北京)</td><td style=\"border:1px solid #e5e7eb;\">{esc(created_sh)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">更新时间(北京)</td><td style=\"border:1px solid #e5e7eb;\">{esc(updated_sh)}</td></tr>
      <tr><td style=\"background:#f9fafb;border:1px solid #e5e7eb;\">来源IP</td><td style=\"border:1px solid #e5e7eb;\">{esc(_safe_text(client_ip or '-', 64))}</td></tr>
    </table>

    <p style=\"margin:14px 0 0;\">
      <a href=\"{esc(admin_orders_link)}\" style=\"display:inline-block;background:#2563eb;color:#fff;text-decoration:none;padding:8px 14px;border-radius:6px;\">打开后台订单管理</a>
      <a href=\"{esc(admin_copy_link)}\" style=\"display:inline-block;background:#059669;color:#fff;text-decoration:none;padding:8px 14px;border-radius:6px;margin-left:8px;\">打开并一键复制</a>
    </p>

    <p style=\"margin:16px 0 6px;font-weight:600;\">复制串（单行）</p>
    <pre style=\"margin:0;padding:10px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:6px;white-space:pre-wrap;word-break:break-all;\">{esc(copy_one_line)}</pre>

    <p style=\"margin:16px 0 6px;font-weight:600;\">复制块（多行）</p>
    <pre style=\"margin:0;padding:10px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:6px;white-space:pre-wrap;word-break:break-all;\">{esc(copy_block)}</pre>
  </body>
</html>
"""

    return subject, plain_text, html_text


def _send_email(subject: str, plain_text: str, html_text: str):
    email_config = SYSTEM_CONFIG.get("email_config", {})
    if not isinstance(email_config, dict) or not bool(email_config.get("enabled")):
        logger.info("[支付] 邮件通知未启用，跳过发送")
        return

    sender_email = str(email_config.get("smtp_user", "") or "").strip()
    sender_password = str(email_config.get("smtp_password", "") or "").strip()
    recipient_email = str(email_config.get("recipient_email", "") or "").strip()
    smtp_server = str(email_config.get("smtp_server", "") or "").strip()

    try:
        smtp_port = int(email_config.get("smtp_port", 465) or 465)
    except Exception:
        smtp_port = 465

    if not (sender_email and sender_password and recipient_email and smtp_server):
        logger.error("[支付] 邮件配置不完整，跳过发送")
        return

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender_email
    message["To"] = recipient_email
    message.attach(MIMEText(plain_text, "plain", "utf-8"))
    message.attach(MIMEText(html_text, "html", "utf-8"))

    try:
        _ = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, recipient_email, message.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, recipient_email, message.as_string())
        logger.info("[支付] 邮件发送成功: %s", recipient_email)
    except Exception as exc:
        logger.error("[支付] 邮件发送失败: %s", exc)


def _send_order_notification(
    order: models.PurchaseOrder,
    event_type: str,
    client_ip: str,
    user_agent: str,
    request_base_url: str,
):
    try:
        subject, plain_text, html_text = _build_order_email(
            order=order,
            event_type=event_type,
            client_ip=client_ip,
            user_agent=user_agent,
            request_base_url=request_base_url,
        )
        _send_email(subject, plain_text, html_text)
        add_runtime_log(
            f"[支付] 邮件通知已发送: 订单={order.order_code}, 事件={event_type}, 版本={_version_label(order.target_version)}, 状态={_status_label(order.status)}"
        )
    except Exception as exc:
        logger.error("[支付] 构建邮件失败: %s", exc)


def _schedule_order_created_notification(order_id: int, client_ip: str, user_agent: str, request_base_url: str):
    def _job():
        time.sleep(ORDER_NOTIFY_DELAY_SECONDS)
        db = database.SessionLocal()
        try:
            order = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == order_id).first()
            if not order:
                logger.info("[支付] 延迟邮件跳过：订单不存在 id=%s", order_id)
                return
            status = str(order.status or "").strip().lower()
            if status not in {"pending", "waiting_verification"}:
                add_runtime_log(
                    f"[支付] 延迟邮件跳过: 订单={order.order_code}, 当前状态={_status_label(status)}"
                )
                return
            _send_order_notification(
                order=order,
                event_type="created",
                client_ip=client_ip,
                user_agent=user_agent,
                request_base_url=request_base_url,
            )
        except Exception as exc:
            logger.error("[支付] 延迟邮件发送异常: %s", exc)
        finally:
            db.close()

    threading.Thread(target=_job, daemon=True, name=f"order-mail-{order_id}").start()


@router.get("/pricing")
async def get_pricing(
    x_device_id: str = Header(None, alias="X-Device-ID"),
    db: Session = Depends(get_db),
):
    if not x_device_id:
        raise HTTPException(status_code=401, detail="Missing Device ID")
    account_store.ensure_device_not_banned(x_device_id)

    user = db.query(models.User).filter(models.User.device_id == x_device_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Device ID")

    used_trials = (
        db.query(models.PurchaseOrder.target_version)
        .filter(models.PurchaseOrder.user_id == user.id)
        .filter(models.PurchaseOrder.duration_days == 3)
        .filter(models.PurchaseOrder.status == "completed")
        .distinct()
        .all()
    )
    used_trial_versions = {row[0] for row in used_trials if row and row[0]}
    return purchase_manager.get_pricing_options(used_trial_versions)


@router.post("/create_order", response_model=schemas.OrderResponse)
async def create_order(
    payload: CreateOrderRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    if not payload.x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")
    account_store.ensure_device_not_banned(payload.x_device_id)

    invite_record_payload: Optional[Dict[str, Any]] = None
    invite_bonus_token = None
    invite_bonus_message = None

    normalized_invite_code = account_store.normalize_invite_code(payload.invite_code or "")
    if normalized_invite_code:
        referral_cfg = SYSTEM_CONFIG.get("referral_config", {})
        if isinstance(referral_cfg, dict) and not bool(referral_cfg.get("enabled", True)):
            raise HTTPException(status_code=400, detail="当前未开启邀请码活动")

        accounts = account_store.load_accounts()
        invitee_username, invitee_account = account_store.get_account_by_device_id(
            payload.x_device_id,
            accounts=accounts,
        )
        if not invitee_username or not invitee_account:
            raise HTTPException(status_code=403, detail="请先注册账号后再使用邀请码")

        inviter_username, inviter_account, normalized_invite_code = account_store.find_account_by_invite_code(
            normalized_invite_code,
            accounts=accounts,
        )
        if not inviter_username or not inviter_account:
            raise HTTPException(status_code=400, detail="邀请码无效，请检查后重试")

        inviter_device_id = str(inviter_account.get("device_id", "")).strip()
        if not inviter_device_id:
            raise HTTPException(status_code=400, detail="邀请码无效，请联系管理员")
        if inviter_device_id == payload.x_device_id:
            raise HTTPException(status_code=400, detail="不能填写自己的邀请码")

        can_apply, reason = account_store.can_apply_invite_for_device(payload.x_device_id)
        if not can_apply:
            raise HTTPException(status_code=400, detail=reason or "当前账号暂不可使用邀请码")

        invite_record_payload = {
            "invite_code": normalized_invite_code,
            "inviter_username": inviter_username,
            "inviter_device_id": inviter_device_id,
            "reward_days": _referral_reward_days(),
        }

    user = user_service.get_or_create_user(db, payload.x_device_id)

    existing_open_order = (
        db.query(models.PurchaseOrder)
        .filter(models.PurchaseOrder.user_id == user.id)
        .filter(models.PurchaseOrder.status.in_(["pending", "waiting_verification"]))
        .order_by(models.PurchaseOrder.created_at.desc())
        .first()
    )
    if existing_open_order:
        raise HTTPException(
            status_code=400,
            detail=f"你有未完成订单（{existing_open_order.order_code}），请先完成或取消后再发起新订单",
        )

    duration_key = _duration_key_from_months(payload.duration_months)
    pricing = purchase_manager.calculate_price(payload.target_version, duration_key)
    if not pricing:
        raise HTTPException(status_code=400, detail="Invalid version or duration")

    if purchase_manager.is_three_day_trial(pricing.get("days", 0)):
        used = (
            db.query(models.PurchaseOrder)
            .filter(models.PurchaseOrder.user_id == user.id)
            .filter(models.PurchaseOrder.target_version == payload.target_version)
            .filter(models.PurchaseOrder.duration_days == 3)
            .filter(models.PurchaseOrder.status == "completed")
            .first()
        )
        if used:
            raise HTTPException(status_code=400, detail="该版本3天体验已购买过，不可重复购买")

    code = purchase_manager.generate_order_code()

    order = models.PurchaseOrder(
        user_id=user.id,
        order_code=code,
        amount=pricing["price"],
        target_version=payload.target_version,
        duration_days=pricing["days"],
        status="pending",
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    add_runtime_log(
        f"[支付] 订单已创建: code={order.order_code}, device={user.device_id}, version={_version_label(order.target_version)}, days={order.duration_days}, amount={order.amount}"
    )

    if invite_record_payload:
        ok, reason, invite_record = account_store.bind_order_invite(
            order_code=order.order_code,
            invite_code=invite_record_payload["invite_code"],
            inviter_username=invite_record_payload["inviter_username"],
            inviter_device_id=invite_record_payload["inviter_device_id"],
            invitee_device_id=user.device_id,
            reward_days=invite_record_payload["reward_days"],
        )
        if ok and invite_record:
            invite_bonus_token = str(invite_record.get("bonus_token", "")).strip() or None
            invite_bonus_message = f"邀请码已生效，订单审核通过后将赠送 {invite_record_payload['reward_days']} 天会员权益。"
            if invite_bonus_token:
                invite_bonus_message += f" 赠送口令：{invite_bonus_token}"
            add_runtime_log(
                f"[支付] 邀请绑定成功: order={order.order_code}, inviter={invite_record_payload['inviter_username']}, invitee={user.device_id}, reward_days={invite_record_payload['reward_days']}"
            )
        else:
            invite_bonus_message = f"订单已创建，但邀请码绑定失败：{reason or '未知错误'}"

    client_ip = http_request.client.host if http_request.client else ""
    user_agent = http_request.headers.get("user-agent", "")
    request_base_url = _request_base_url(http_request)
    _schedule_order_created_notification(order.id, client_ip, user_agent, request_base_url)
    add_runtime_log(
        f"[支付] 新订单邮件将延迟 {ORDER_NOTIFY_DELAY_SECONDS} 秒发送: {order.order_code}"
    )

    return {
        "order_code": order.order_code,
        "amount": order.amount,
        "status": order.status,
        "invite_bonus_token": invite_bonus_token,
        "invite_bonus_message": invite_bonus_message,
    }


@router.post("/confirm_payment")
async def confirm_payment(
    request: Request,
    order_code: str = Body(..., embed=True),
    x_device_id: str = Header(None, alias="X-Device-ID"),
    db: Session = Depends(get_db),
):
    if not x_device_id:
        raise HTTPException(status_code=401, detail="Missing Device ID")
    account_store.ensure_device_not_banned(x_device_id)

    order = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.order_code == order_code).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not order.user or order.user.device_id != x_device_id:
        raise HTTPException(status_code=403, detail="Order access denied")

    if order.status != "pending":
        if order.status == "waiting_verification":
            return {"status": "success", "message": "Already waiting for verification"}
        if order.status == "completed":
            raise HTTPException(status_code=400, detail="Order already completed")

    order.status = "waiting_verification"
    db.commit()

    client_ip = request.client.host if request.client else ""
    add_runtime_log(f"[支付] 用户已确认订单: {order.order_code} -> 等待审核, IP={client_ip}")
    return {"status": "success", "message": "Waiting for verification"}


@router.post("/cancel_order")
async def cancel_order(
    request: Request,
    order_code: str = Body(..., embed=True),
    x_device_id: str = Header(None, alias="X-Device-ID"),
    db: Session = Depends(get_db),
):
    if not x_device_id:
        raise HTTPException(status_code=401, detail="Missing Device ID")
    account_store.ensure_device_not_banned(x_device_id)

    order = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.order_code == order_code).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not order.user or order.user.device_id != x_device_id:
        raise HTTPException(status_code=403, detail="Order access denied")

    current_status = str(order.status or "").strip().lower()
    if current_status not in {"pending", "waiting_verification"}:
        if current_status == "completed":
            raise HTTPException(status_code=400, detail="Order already completed and cannot be cancelled")
        if current_status == "cancelled":
            return {"status": "success", "message": "Order already cancelled"}
        raise HTTPException(status_code=400, detail=f"Order in status '{current_status}' cannot be cancelled")

    order.status = "cancelled"
    db.commit()

    account_store.update_order_invite_status(order.order_code, "cancelled", reason="order_cancelled")
    add_runtime_log(f"[支付] 订单已取消: {order.order_code}")

    client_ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")
    request_base_url = _request_base_url(request)
    _send_order_notification(
        order=order,
        event_type="cancelled",
        client_ip=client_ip,
        user_agent=user_agent,
        request_base_url=request_base_url,
    )

    return {"status": "success"}


@router.get("/orders")
async def list_my_orders(
    x_device_id: str = Header(None, alias="X-Device-ID"),
    limit: int = 20,
    db: Session = Depends(get_db),
):
    if not x_device_id:
        raise HTTPException(status_code=401, detail="Missing Device ID")
    account_store.ensure_device_not_banned(x_device_id)

    user = db.query(models.User).filter(models.User.device_id == x_device_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Device ID")

    safe_limit = max(1, min(int(limit or 20), 100))
    orders = (
        db.query(models.PurchaseOrder)
        .filter(models.PurchaseOrder.user_id == user.id)
        .order_by(models.PurchaseOrder.created_at.desc())
        .limit(safe_limit)
        .all()
    )
    current_open = next(
        (o for o in orders if str(o.status or "").strip().lower() in {"pending", "waiting_verification"}),
        None,
    )

    return {
        "current_order": _serialize_order(current_open) if current_open else None,
        "history": [_serialize_order(o) for o in orders],
    }
