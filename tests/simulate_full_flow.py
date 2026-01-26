import requests
import time
import subprocess
import sys
import os
import signal
from pathlib import Path
import json

# Configuration
BASE_URL = "http://127.0.0.1:8000"
DEVICE_ID = "test_user_sim_001"
PYTHON_CMD = sys.executable
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def start_server():
    print("[Test] Starting Server...")
    # Assume run.py is in Server-Version/run.py or we run uvicorn direct
    # Better to run backend module
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "backend")
    
    cmd = [PYTHON_CMD, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
    # Run from backend dir
    # Use PIPE to capture output in case of error, but we need non-blocking read or just let it print
    # Let's let it print to console for debugging
    proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT / "backend", env=env)
    return proc

def wait_for_server():
    print("[Test] Waiting for server to be ready...")
    for i in range(20): # Increased wait time
        try:
            r = requests.get(f"{BASE_URL}/api/status")
            if r.status_code == 200:
                print("[Test] Server is UP!")
                return True
        except:
            pass
        time.sleep(2)
    return False

def get_admin_token():
    token_path = PROJECT_ROOT / "backend" / "data" / "admin_token.txt"
    if token_path.exists():
        with open(token_path, "r") as f:
            return f.read().strip()
    return None

def run_simulation():
    server_proc = start_server()
    
    try:
        if not wait_for_server():
            print("[Test] Server failed to start.")
            return

        print("\n=== 1. User Registration (Auto) ===")
        # 1. Login (Auto-Register)
        # Endpoint is POST /api/auth/login
        login_payload = {"device_id": DEVICE_ID}
        res = requests.post(f"{BASE_URL}/api/auth/login", json=login_payload)
        assert res.status_code == 200
        user_data = res.json()
        print(f"User Created: ID={user_data['id']}, Version={user_data['version']}")
        assert user_data['version'] == "trial"

        print("\n=== 2. Create Order ===")
        # 2. Create Order
        order_payload = {
            "target_version": "advanced",
            "duration_months": 1 # 1 month
        }
        # Assuming payload for CreateOrderRequest
        # Check backend/app/api/payment.py CreateOrderRequest model:
        # x_device_id, target_version, duration_months
        payload = {
            "x_device_id": DEVICE_ID,
            "target_version": "advanced",
            "duration_months": 1
        }
        res = requests.post(f"{BASE_URL}/api/payment/create_order", json=payload)
        assert res.status_code == 200
        order_data = res.json()
        order_code = order_data["order_code"]
        amount = order_data["amount"]
        print(f"Order Created: Code={order_code}, Amount={amount}")

        print("\n=== 3. Confirm Payment (User Action) ===")
        # 3. Confirm
        # confirm_payment(order_code=Body)
        res = requests.post(f"{BASE_URL}/api/payment/confirm_payment", json={"order_code": order_code})
        assert res.status_code == 200
        print("Payment Confirmed by User. Status: Waiting Verification")

        print("\n=== 4. Admin Approval ===")
        # 4. Get Admin Token
        admin_token = get_admin_token()
        if not admin_token:
            print("[Error] Could not find admin token file.")
            return
        
        admin_headers = {"X-Admin-Token": admin_token}
        
        # Verify Order in Admin List
        res = requests.get(f"{BASE_URL}/api/admin/orders?status=waiting_verification", headers=admin_headers)
        assert res.status_code == 200
        orders = res.json()
        target_order = next((o for o in orders if o["order_code"] == order_code), None)
        assert target_order is not None
        print(f"Admin saw order: {target_order['order_code']}")

        # Approve
        approve_payload = {"order_code": order_code, "action": "approve"}
        res = requests.post(f"{BASE_URL}/api/admin/orders/approve", json=approve_payload, headers=admin_headers)
        assert res.status_code == 200
        print("Admin Approved Order.")

        print("\n=== 5. Verify User Upgrade ===")
        # 5. Check User Profile again
        res = requests.post(f"{BASE_URL}/api/auth/login", json=login_payload)
        user_data = res.json()
        print(f"User Version: {user_data['version']}")
        print(f"User Expires: {user_data['expires_at']}")
        assert user_data['version'] == "advanced"
        
        print("\n=== 6. Admin Add Time (Bonus) ===")
        # 6. Add 5 Days (7200 mins)
        add_time_payload = {"user_id": user_data['id'], "minutes": 7200}
        res = requests.post(f"{BASE_URL}/api/admin/users/add_time", json=add_time_payload, headers=admin_headers)
        assert res.status_code == 200
        print("Admin Added Extra Time.")
        
        # Check again
        res = requests.post(f"{BASE_URL}/api/auth/login", json=login_payload)
        user_data = res.json()
        print(f"Final Expiry: {user_data['expires_at']}")

        print("\n\n✅ ALL TESTS PASSED SUCCESSFULLY!")

    except Exception as e:
        print(f"\n❌ TEST FAILED: {str(e)}")
        import traceback
        traceback.print_exc()
    
    finally:
        print("[Test] Stopping Server...")
        server_proc.terminate()
        server_proc.wait()

if __name__ == "__main__":
    run_simulation()
