import uvicorn
import webbrowser
import threading
import time
import sys
import os
import socket
import http.server
import socketserver
from functools import partial

# Add root directory to path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)
sys.path.append(os.path.join(ROOT_DIR, "backend"))

# Import backend app
from app.main import app

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]

def run_backend(host, port):
    uvicorn.run(app, host=host, port=port, log_level="error")

def run_frontend(host, port, directory):
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    # Allow address reuse
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer((host, port), handler) as httpd:
            print(f"前端服务启动于: http://{host}:{port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"前端服务启动失败: {e}")

def main():
    backend_port = 8000 # Fixed port for config.js
    frontend_port = find_free_port()
    host = "127.0.0.1"
    
    frontend_dir = os.path.join(ROOT_DIR, "frontend")
    
    print(f"正在启动涨停狙击手商业版...")
    print(f"后端端口: {backend_port}")
    print(f"前端端口: {frontend_port}")
    
    # Start Backend
    t_backend = threading.Thread(target=run_backend, args=(host, backend_port), daemon=True)
    t_backend.start()
    
    # Start Frontend
    t_frontend = threading.Thread(target=run_frontend, args=(host, frontend_port, frontend_dir), daemon=True)
    t_frontend.start()
    
    # Open Browser
    url = f"http://{host}:{frontend_port}/index.html"
    print(f"即将打开浏览器: {url}")
    time.sleep(3) 
    webbrowser.open(url)
    
    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("停止服务...")
        sys.exit(0)

if __name__ == "__main__":
    main()
