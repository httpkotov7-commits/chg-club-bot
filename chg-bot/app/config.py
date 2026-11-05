import os
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Публичный https-адрес этого сервиса (Railway даёт домен вида https://xxx.up.railway.app)
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")

_db = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data.db")
if _db.startswith("postgres://"):
    _db = _db.replace("postgres://", "postgresql+asyncpg://", 1)
elif _db.startswith("postgresql://"):
    _db = _db.replace("postgresql://", "postgresql+asyncpg://", 1)
DATABASE_URL = _db

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}
PORT = int(os.getenv("PORT", "8080"))
RUN_BOT = os.getenv("RUN_BOT", "1") == "1"
# Принимать «тестовые» коды без базы (контрольная сумма). На проде держать 0.
DEMO_CODES = os.getenv("DEMO_CODES", "0") == "1"
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tashkent"))
INITDATA_MAX_AGE = int(os.getenv("INITDATA_MAX_AGE", str(24 * 3600)))
WEBAPP_DIR = os.getenv("WEBAPP_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp"))
