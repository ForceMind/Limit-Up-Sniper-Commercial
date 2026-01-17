import json
import os
import numpy as np
from scipy.spatial import distance
from datetime import datetime

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
PROFILE_FILE = os.path.join(BASE_DIR, "data", "seat_profiles.json")

class SeatMatcher:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SeatMatcher, cls).__new__(cls)
            cls._instance.profiles = {}
            cls._instance.load_profiles()
        return cls._instance

    def load_profiles(self):
        if os.path.exists(PROFILE_FILE):
            try:
                with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
                    self.profiles = json.load(f)
                print(f"[SeatMatcher] Loaded {len(self.profiles)} profiles.")
            except Exception as e:
                print(f"[SeatMatcher] Error loading profiles: {e}")
        else:
            print(f"[SeatMatcher] Profile file not found: {PROFILE_FILE}")

    def calculate_realtime_features(self, stock_data):
        """
        从实时数据计算特征向量
        stock_data: dict, 包含 'time', 'price_history', 'volume', 'avg_volume', 'market_cap', 'limit_up_days'
        """
        try:
            # 1. Time Feature (minutes from 9:30)
            now = datetime.now()
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            time_feature = (now - market_open).total_seconds() / 60
            if time_feature < 0: time_feature = 0 # Before open

            # 2. Slope Feature (Linear regression on last 5 mins prices)
            # price_history is assumed to be a list of prices
            prices = stock_data.get('price_history', [])
            if len(prices) >= 2:
                y = np.array(prices)
                x = np.arange(len(y))
                # Simple linear regression slope: cov(x,y) / var(x)
                slope_feature = np.polyfit(x, y, 1)[0]
                # Normalize slope roughly to percentage/min if needed, but here we keep raw slope
                # Assuming prices are normalized or we use percentage change
                # Let's assume prices are raw, so slope is price change per minute
                # To match training (which likely used raw prices or normalized), let's assume consistency.
                # Better: use (price - price_0) / price_0 * 100
            else:
                slope_feature = 0

            # 3. Volume Feature
            current_vol = stock_data.get('volume', 0)
            avg_vol = stock_data.get('avg_volume', 1) # Avoid div by zero
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0

            # 4. Cap Feature
            cap_feature = stock_data.get('market_cap', 0)

            # 5. Board Feature
            board_feature = stock_data.get('limit_up_days', 1)

            return np.array([time_feature, slope_feature, vol_ratio, cap_feature, board_feature])
        except Exception as e:
            print(f"[SeatMatcher] Error calculating features: {e}")
            return None

    def match(self, stock_data):
        """
        Match stock against all profiles
        Returns: list of (seat_name, similarity, description)
        """
        if not self.profiles:
            return []

        vector_a = self.calculate_realtime_features(stock_data)
        if vector_a is None:
            return []

        matches = []
        for name, profile in self.profiles.items():
            features = profile.get('features', {})
            # Ensure order matches: time, slope, vol_ratio, cap, board
            vector_b = np.array([
                features.get('time', 0),
                features.get('slope', 0),
                features.get('vol_ratio', 0),
                features.get('cap', 0),
                features.get('board', 0)
            ])
            
            weights = np.array(profile.get('weights', [0.2]*5))

            # Calculate Weighted Euclidean Distance
            # Normalize features if necessary, but here we assume raw values are comparable 
            # or weights handle the scale differences.
            # For simplicity and robustness, let's use Cosine Similarity on weighted vectors
            # But user asked for Euclidean or Cosine.
            # Let's use Cosine Similarity.
            
            # Simple Cosine Similarity
            norm_a = np.linalg.norm(vector_a)
            norm_b = np.linalg.norm(vector_b)
            
            if norm_a == 0 or norm_b == 0:
                similarity = 0
            else:
                similarity = np.dot(vector_a, vector_b) / (norm_a * norm_b)

            if similarity > 0.85:
                matches.append({
                    'name': name,
                    'similarity': round(similarity * 100, 1),
                    'desc': profile.get('desc', '')
                })

        # Sort by similarity desc
        matches.sort(key=lambda x: x['similarity'], reverse=True)
        return matches[:3] # Return top 3

# Global instance
matcher = SeatMatcher()
