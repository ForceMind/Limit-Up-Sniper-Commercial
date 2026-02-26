from fastapi import APIRouter, Depends, HTTPException, Body, Request, Header
from sqlalchemy.orm import Session
from app.db import database, models, schemas
from app.core import purchase_manager, user_service, account_store
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.core.config_manager import SYSTEM_CONFIG
from app.core.runtime_logs import add_runtime_log

# Configure Logging
logger = logging.getLogger("payment")


def send_email_notification(order, client_ip):
    """Send email notification using configured SMTP server."""
    logger.info("======== NEW PAYMENT NOTIFICATION ========")
    logger.info(f"Order: {order.order_code}")
    logger.info(f"Amount: {order.amount}")
    add_runtime_log(f"[PAY] New order notify: {order.order_code}, amount={order.amount}, ip={client_ip}")

    # 1. Log to console/file
    print(f"[Email Logic] Admin notified for order {order.order_code} from {client_ip}")

    # 2. Check if email is enabled
    email_config = SYSTEM_CONFIG.get("email_config", {})
    if not email_config.get("enabled"):
        logger.info("Email notification disabled in config.")
        return

    # 3. Try to send email
    try:
        sender_email = email_config.get("smtp_user")
        sender_password = email_config.get("smtp_password")
        recipient_email = email_config.get("recipient_email")
        smtp_server = email_config.get("smtp_server")
        smtp_port = int(email_config.get("smtp_port", 465))

        if not (sender_email and sender_password and recipient_email and smtp_server):
            logger.error("Email config incomplete.")
            return

        message = MIMEMultipart("alternative")
        message["Subject"] = f"New Order: {order.order_code} - ¥{order.amount}"
        message["From"] = sender_email
        message["To"] = recipient_email

        text = f"""
        New Order Received!

        Code: {order.order_code}
        Amount: ¥{order.amount}
        Version: {order.target_version}
        Duration: {order.duration_days} days
        User IP: {client_ip}
        Time: {datetime.now()}

        Please check Admin Panel to approve.
        """

        message.attach(MIMEText(text, "plain"))

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

        logger.info(f"Email sent successfully to {recipient_email}")

    except Exception as e:
        logger.error(f"Failed to send email: {e}")


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
    request: CreateOrderRequest,
    db: Session = Depends(get_db),
):
    if not request.x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")
    account_store.ensure_device_not_banned(request.x_device_id)

    invite_record_payload = None
    invite_bonus_token = None
    invite_bonus_message = None
    normalized_invite_code = account_store.normalize_invite_code(request.invite_code or "")
    if normalized_invite_code:
        referral_cfg = SYSTEM_CONFIG.get("referral_config", {})
        if isinstance(referral_cfg, dict) and not bool(referral_cfg.get("enabled", True)):
            raise HTTPException(status_code=400, detail="当前未开启邀请码活动")
        accounts = account_store.load_accounts()
        invitee_username, invitee_account = account_store.get_account_by_device_id(
            request.x_device_id,
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
        if inviter_device_id == request.x_device_id:
            raise HTTPException(status_code=400, detail="不能填写自己的邀请码")

        can_apply, reason = account_store.can_apply_invite_for_device(request.x_device_id)
        if not can_apply:
            raise HTTPException(status_code=400, detail=reason or "当前账号暂不可使用邀请码")

        invite_record_payload = {
            "invite_code": normalized_invite_code,
            "inviter_username": inviter_username,
            "inviter_device_id": inviter_device_id,
            "reward_days": _referral_reward_days(),
        }

    # Get User
    user = user_service.get_or_create_user(db, request.x_device_id)

    # Calculate Price
    duration_key = _duration_key_from_months(request.duration_months)
    pricing = purchase_manager.calculate_price(request.target_version, duration_key)
    if not pricing:
        raise HTTPException(status_code=400, detail="Invalid version or duration")

    # 每个版本3天体验只能购买一次（服务端强校验）
    if purchase_manager.is_three_day_trial(pricing.get("days", 0)):
        used = (
            db.query(models.PurchaseOrder)
            .filter(models.PurchaseOrder.user_id == user.id)
            .filter(models.PurchaseOrder.target_version == request.target_version)
            .filter(models.PurchaseOrder.duration_days == 3)
            .filter(models.PurchaseOrder.status == "completed")
            .first()
        )
        if used:
            raise HTTPException(status_code=400, detail="该版本3天体验已购买过，不可重复购买")

    # Generate Code
    code = purchase_manager.generate_order_code()

    # Create Order
    order = models.PurchaseOrder(
        user_id=user.id,
        order_code=code,
        amount=pricing["price"],
        target_version=request.target_version,
        duration_days=pricing["days"],
        status="pending",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    add_runtime_log(
        f"[PAY] Order created: code={order.order_code}, device={user.device_id}, version={order.target_version}, days={order.duration_days}, amount={order.amount}"
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
            invite_bonus_message = (
                f"邀请码已生效，订单审核通过后将赠送 {invite_record_payload['reward_days']} 天会员权益。"
            )
            if invite_bonus_token:
                invite_bonus_message += f" 赠送口令：{invite_bonus_token}"
            add_runtime_log(
                f"[PAY] Invite bound: order={order.order_code}, inviter={invite_record_payload['inviter_username']}, invitee={user.device_id}, reward_days={invite_record_payload['reward_days']}"
            )
        else:
            invite_bonus_message = f"订单已创建，但邀请码绑定失败：{reason or '未知错误'}"

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
        # Allow re-confirm if status is waiting_verification, but block if completed
        if order.status == "waiting_verification":
            return {"status": "success", "message": "Already waiting for verification"}
        if order.status == "completed":
            raise HTTPException(status_code=400, detail="Order already completed")

    order.status = "waiting_verification"
    db.commit()
    add_runtime_log(f"[PAY] Order confirmed by user: {order.order_code} -> waiting_verification")

    # Send Email Notification to Admin
    client_ip = request.client.host
    send_email_notification(order, client_ip)

    return {"status": "success", "message": "Waiting for verification"}


@router.post("/cancel_order")
async def cancel_order(
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

    order.status = "cancelled"
    db.commit()
    account_store.update_order_invite_status(order.order_code, "cancelled", reason="order_cancelled")
    add_runtime_log(f"[PAY] Order cancelled: {order.order_code}")
    return {"status": "success"}
