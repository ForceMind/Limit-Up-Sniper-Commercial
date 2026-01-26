import os
import shutil
import zipfile
import re
from pathlib import Path

def package_frontend():
    print("="*50)
    print("Limit-Up Sniper Commercial - Frontend Packager")
    print("="*50)

    # 1. Ask for configuration
    print("Please enter the Backend Server API URL for the Client Version.")
    print("Examples: http://1.2.3.4:8000 or https://api.mydomain.com")
    print("Press Enter to use default local logic (auto-detect or localhost).")
    
    api_url = input("Server URL [Default]: ").strip()

    # Paths
    base_dir = Path(__file__).resolve().parent.parent
    frontend_dir = base_dir / "frontend"
    dist_dir = base_dir / "dist"
    
    # Clean/Create dist
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir()
    
    # Create sub-folders for Client and Server versions
    client_dist = dist_dir / "client_dist"
    server_dist = dist_dir / "server_dist"
    client_dist.mkdir()
    server_dist.mkdir()

    # --- Copy Files for Client ---
    print("\n[1/3] Preparing Client Distribution...")
    client_files = ["index.html", "help.html", "lhb.html", "config.js"]
    shutil.copytree(frontend_dir / "static", client_dist / "static")
    
    # Copy main HTMLs
    for f in client_files:
        src = frontend_dir / f
        if src.exists():
            shutil.copy2(src, client_dist / f)

    # Modify config.js for Client
    config_path = client_dist / "config.js"
    if api_url:
        new_content = f'const API_BASE_URL = "{api_url}";'
    else:
        # Keep original logic if no URL provided
        pass # Original file already copied
        
    if api_url:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(f"// Generated Config for Client\n")
            f.write(f"{new_content}\n")
            f.write(f"console.log('Limit-Up Sniper Client Connected to:', API_BASE_URL);\n")

    # Zip Client
    shutil.make_archive(base_dir / "dist_client_package", 'zip', client_dist)
    print(f"   -> Created dist_client_package.zip")

    # --- Copy Files for Server ---
    print("\n[2/3] Preparing Server Distribution...")
    # Server needs everything including admin
    shutil.copytree(frontend_dir, server_dist, dirs_exist_ok=True)
    
    # Zip Server
    shutil.make_archive(base_dir / "dist_server_package", 'zip', server_dist)
    print(f"   -> Created dist_server_package.zip")

    print("\n[3/3] Done!")
    print(f"Packages located in: {dist_dir.parent}")

if __name__ == "__main__":
    package_frontend()
