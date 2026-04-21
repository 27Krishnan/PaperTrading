import pytz
from datetime import datetime

IST = pytz.timezone("Asia/Kolkata")

def get_now_ist() -> datetime:
    """Returns current datetime in IST (Asia/Kolkata)"""
    return datetime.now(IST)
