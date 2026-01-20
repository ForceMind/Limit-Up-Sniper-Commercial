import requests
import uuid
import time
import sys

BASE_URL = "http://127.0.0.1:8000"
ADMIN_TOKEN = "admin-secret-8888"

def test_flow():
    device_id = f"test_device_{uuid.uuid4().hex[:8]}"
    print(f"Testing with device_id: {device_id}")
    
    # 1. Get User Info (Auto Create)
    print("\n[Step 1] Initialize User (Trial)")
    headers = {"X-Device-ID": device_id}
    try:
        resp = requests.get(f"{BASE_URL}/api/user/info", headers=headers)
        if resp.status_code != 200:
            print(f"Failed to connect or get user: {resp.status_code} {resp.text}")
            return
        user_data = resp.json()
        print("User Info:", user_data)
        assert user_data['version'] == 'trial'
        
        # 2. Need to check if raid endpoint permissions work
        # Accessing /api/raid/limit-up requires check_raid_permission
        print("\n[Step 2] Consume Quota (Raid)")
        # Trial user has 10 daily raids.
        for i in range(3):
            resp = requests.get(f"{BASE_URL}/api/raid/limit-up", headers=headers)
            print(f"Raid check {i+1}: {resp.status_code}")
            
        resp = requests.get(f"{BASE_URL}/api/user/info", headers=headers)
        user_data = resp.json()
        print("User Info quota:", user_data['remaining_raid'])
        
        # 3. Create Order
        print("\n[Step 3] Create Order (Advanced Version)")
        order_payload = {
            "target_version": "advanced",
            "duration_months": 1,
            "x_device_id": device_id
        }
        resp = requests.post(f"{BASE_URL}/api/payment/create_order", json=order_payload, headers=headers) # note endpoint matches payment.py router path? 
        # In main.py: app.include_router(payment.router, prefix="/api/payment", tags=["payment"])
        # In payment.py: @router.post("/create_order")
        # So URL is /api/payment/create_order
        print(f"Create Order Status: {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text)
        
        order_data = resp.json()
        order_code = order_data['order_code']
        print(f"Order Created: {order_code}")
        
        # 4. Admin Approve
        print("\n[Step 4] Admin Approve")
        # Need to know order_code.
        approve_payload = {
            "order_code": order_code,
            "action": "approve"
        }
        admin_headers = {"X-Admin-Token": ADMIN_TOKEN}
        resp = requests.post(f"{BASE_URL}/api/admin/orders/approve", json=approve_payload, headers=admin_headers)
        print(f"Admin Approve Status: {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text)
            
        # 5. Verify Upgrade
        print("\n[Step 5] Verify Upgrade")
        resp = requests.get(f"{BASE_URL}/api/user/info", headers=headers)
        user_data = resp.json()
        print("User Version:", user_data['version'])
        print("User Expires:", user_data['expires_at'])
        
        if user_data['version'] == 'advanced':
            print("SUCCESS: User upgraded to advanced.")
        else:
            print("FAILURE: User not upgraded.")
            
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    test_flow()
