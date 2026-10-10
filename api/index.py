import sys
import os

# Root path add kar rahe hain taake app.py import ho sakay
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

# Vercel Serverless Handler
def handler(request, response):
    return app(request, response)
