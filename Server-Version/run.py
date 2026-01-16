import uvicorn
import os
import sys

# Add the project root directory to sys.path so that 'app' can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    # Use 'app.main:app' string to allow reload to work if needed, 
    # but here we import directly or use string.
    # For production-like behavior with workers, use string.
    # Note: 'workers' > 1 requires the app to be stateless or use external storage.
    # Since we use in-memory state (global vars), we MUST use workers=1.
    # To use multi-core for processing, we rely on ProcessPoolExecutor in the background tasks.
    uvicorn.run(
        "app.main:app", 
        host="0.0.0.0", 
        port=8000, 
        reload=True,
        reload_excludes=["*.json", "*.csv", "*.txt", "*.log", "data/*"]
    )
