import sys
import os

# Root directory ko path mein add karna taake app.py import ho sakay
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
