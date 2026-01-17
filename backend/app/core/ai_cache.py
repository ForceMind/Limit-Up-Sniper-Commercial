import json
import time
import hashlib
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "ai_cache.json"

class AICache:
    def __init__(self):
        self.cache_file = CACHE_FILE
        self.cache = self._load_cache()

    def _load_cache(self):
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_cache(self):
        # Ensure directory exists
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_file, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    def get(self, key, max_age_seconds=86400):
        """
        Get cached data if it exists and is not expired.
        """
        if key in self.cache:
            entry = self.cache[key]
            timestamp = entry.get('timestamp', 0)
            if time.time() - timestamp < max_age_seconds:
                return entry.get('data')
        return None

    def set(self, key, data):
        """
        Set cache data with current timestamp.
        """
        self.cache[key] = {
            'timestamp': int(time.time()),
            'data': data
        }
        self._save_cache()
        
    def cleanup(self, max_age_seconds=604800):
        """
        Remove entries older than max_age_seconds (default 7 days).
        """
        now = time.time()
        initial_count = len(self.cache)
        self.cache = {k: v for k, v in self.cache.items() if now - v.get('timestamp', 0) < max_age_seconds}
        if len(self.cache) < initial_count:
            self._save_cache()
            return initial_count - len(self.cache)
        return 0

    def get_timestamp(self, key):
        if key in self.cache:
            return self.cache[key].get('timestamp', 0)
        return 0

    @staticmethod
    def generate_key(content):
        """Generate MD5 hash for content."""
        if isinstance(content, (dict, list)):
            content = json.dumps(content, sort_keys=True)
        return hashlib.md5(str(content).encode('utf-8')).hexdigest()

ai_cache = AICache()
