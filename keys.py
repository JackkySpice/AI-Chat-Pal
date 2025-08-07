from datetime import datetime, timedelta, timezone

# Demo keys mapping: key string -> expiry datetime (UTC)
DEMO_KEYS = {
    "DEMO-KEY-1D": datetime.now(timezone.utc) + timedelta(days=1),
    "DEMO-KEY-7D": datetime.now(timezone.utc) + timedelta(days=7),
    "DEMO-KEY-30D": datetime.now(timezone.utc) + timedelta(days=30),
}