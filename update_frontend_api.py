import os
import re

FRONTEND_DIR = r"e:\Privy\Limit-Up-Sniper-Commercial\frontend"

def process_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Replacements
    new_content = content
    
    # 1. API calls
    # Handle single quote
    new_content = new_content.replace("fetch('/api", "fetch(API_BASE_URL + '/api")
    # Handle backtick
    new_content = new_content.replace("fetch(`/api", "fetch(API_BASE_URL + `/api")
    # Handle double quote if any (rare but possible)
    new_content = new_content.replace('fetch("/api', 'fetch(API_BASE_URL + "/api')

    # 2. Static paths and Links
    new_content = new_content.replace('href="/static', 'href="static')
    new_content = new_content.replace('src="/static', 'src="static')
    new_content = new_content.replace('href="/lhb"', 'href="lhb.html"')
    new_content = new_content.replace('href="/help"', 'href="help.html"')
    new_content = new_content.replace('href="/"', 'href="index.html"')

    # 3. Jinja2 cleanup (if missed)
    new_content = new_content.replace('{% raw %}', '')
    new_content = new_content.replace('{% endraw %}', '')

    # 4. Inject config.js
    if 'src="config.js"' not in new_content:
        # Try inserting before marked.js or just end of head
        if '</head>' in new_content:
            new_content = new_content.replace('</head>', '    <script src="config.js"></script>\n</head>')
        else:
             print(f"Warning: No </head> found in {filepath}")

    if content != new_content:
        print(f"Updating {filepath}...")
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
    else:
        print(f"No changes for {filepath}")

for filename in os.listdir(FRONTEND_DIR):
    if filename.endswith(".html"):
        process_file(os.path.join(FRONTEND_DIR, filename))
