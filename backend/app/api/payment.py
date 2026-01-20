from fastapi import APIRouter, Depends, HTTPException, Body, Request
from sqlalchemy.orm import Session
from app.db import database, models, schemas
from app.core import purchase_manager, user_service
from datetime import datetime
from pydantic import BaseModel
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.core.config_manager import SYSTEM_CONFIG

# Configure Logging
logger = logging.getLogger("payment")

def send_email_notification(order, client_ip):
    """
    Send email notification using configured SMTP server.
    """
    logger.info(f"======== NEW PAYMENT NOTIFICATION ========")
    logger.info(f"Order: {order.order_code}")
    logger.info(f"Amount: {order.amount}")
    
    # 1. Log to console/file
    print(f"📧 [Email Logic] Admin notified for order {order.order_code} from {client_ip}")

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
        message["Subject"] = f"New Order: {order.order_code} - ￥{order.amount}"
        message["From"] = sender_email
        message["To"] = recipient_email

        text = f"""
        New Order Received!
        
        Code: {order.order_code}
        Amount: ￥{order.amount}
        Version: {order.target_version}
        Duration: {order.duration_days} days
        User IP: {client_ip}
        Time: {datetime.now()}
        
        Please check Admin Panel to approve.
        """
        
        message.attach(MIMEText(text, "plain"))

        # Use SSL
        context = ssl.create_default_context() if 'ssl' in globals() else None
        
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


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/pricing")
async def get_pricing():
    return purchase_manager.get_pricing_options()

@router.post("/create_order", response_model=schemas.OrderResponse)
async def create_order(
    request: CreateOrderRequest,
    db: Session = Depends(get_db)
):
    # Get User
    user = user_service.get_or_create_user(db, request.x_device_id)
    
    # Calculate Price
    duration_key = "1m"
    if request.duration_months == 0:
        duration_key = "3d"
    elif request.duration_months == 3:
        duration_key = "3m"
    elif request.duration_months == 6:
        duration_key = "6m"
    elif request.duration_months == 12:
        duration_key = "12m"
    
    pricing = purchase_manager.calculate_price(request.target_version, duration_key)
    if not pricing:
        raise HTTPException(status_code=400, detail="Invalid version or duration")
    
    # Generate Code
    code = purchase_manager.generate_order_code()
    
    # Create Order
    order = models.PurchaseOrder(
        user_id=user.id,
        order_code=code,
        amount=pricing["price"],
        target_version=request.target_version,
        duration_days=pricing["days"],
        status="pending"
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    
    return {
        "order_code": order.order_code,
        "amount": order.amount,
        "status": order.status
    }

@router.post("/confirm_payment")
async def confirm_payment(
    request: Request,
    order_code: str = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    order = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.order_code == order_code).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.status != "pending":
         # Allow re-confirm if status is pending, but block if completed
         if order.status == "waiting_verification":
             return {"status": "success", "message": "Already waiting for verification"}
         if order.status == "completed":
             raise HTTPException(status_code=400, detail="Order already completed")
         
    order.status = "waiting_verification"
    db.commit()
    
    # Send Email Notification to Admin
    client_ip = request.client.host
    send_email_notification(order, client_ip)
    
    return {"status": "success", "message": "Waiting for verification"}

@router.post("/cancel_order")
async def cancel_order(
    order_code: str = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    order = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.order_code == order_code).first()
    if not order:
         raise HTTPException(status_code=404, detail="Order not found")
    
    order.status = "cancelled"
    db.commit()
    return {"status": "success"}
