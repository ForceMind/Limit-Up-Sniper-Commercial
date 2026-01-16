import uvicorn
import webbrowser
import threading
import time
import sys
import os
import socket

# Add root directory to path so we can import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]

def start_browser(url):
    time.sleep(1.5) # Wait for server to start
    webbrowser.open(url)

def main():
    port = find_free_port()
    host = "127.0.0.1"
    url = f"http://{host}:{port}"
    
    print(f"正在启动涨停狙击手，地址：{url}")
    print("按下 Ctrl+C 退出程序")
    
    # Start browser in a separate thread
    threading.Thread(target=start_browser, args=(url,), daemon=True).start()
    
    # Start server
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()