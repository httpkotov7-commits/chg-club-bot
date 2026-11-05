from sqlalchemy import (MetaData, Table, Column, BigInteger, Integer, String, Text, DateTime, Boolean,
                        func, select, insert)
from sqlalchemy.ext.asyncio import create_async_engine

from .config import DATABASE_URL

md = MetaData()

users = Table(
    "users", md,
    Column("id", BigInteger, primary_key=True),            # telegram user id = chat id
    Column("first_name", String(128), default=""),
    Column("last_name", String(128), default=""),
    Column("username", String(64)),
    Column("lang", String(2), default="ru"),
    Column("points", Integer, nullable=False, default=0),
    Column("lifetime", Integer, nullable=False, default=0),  # уровень считается от всех накопленных баллов
    Column("birthday", String(10)),
    Column("survey_done", Boolean, nullable=False, default=False),
    Column("onboarded", Boolean, nullable=False, default=False),
    Column("fails", Integer, nullable=False, default=0),
    Column("fails_since", DateTime(timezone=True)),
    Column("lock_until", DateTime(timezone=True)),
    Column("banned", Boolean, nullable=False, default=False),       # скрыт из рейтинга, не может копить
    Column("blocked_bot", Boolean, nullable=False, default=False),  # пользователь заблокировал бота
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_seen", DateTime(timezone=True)),
)

codes = Table(
    "codes", md,
    Column("code", String(16), primary_key=True),          # хранится без префикса и дефисов: 8 символов
    Column("gift_prize", String(32)),                      # если в коде «спрятан подарок»
    Column("used_by", BigInteger, index=True),
    Column("used_at", DateTime(timezone=True), index=True),
    Column("points", Integer),
)

ledger = Table(  # журнал всех начислений и списаний — для аудита и аналитики
    "ledger", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, index=True),
    Column("delta", Integer, nullable=False),
    Column("reason", String(32)),
    Column("ref", String(64)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

plays = Table(
    "plays", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, index=True),
    Column("game", String(16)),
    Column("day", String(10), index=True),
    Column("attempt", Integer),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("score", Integer),
    Column("points", Integer, nullable=False, default=0),
    Column("flag", String(32)),                            # причина, если результат отклонён
)

scratches = Table(
    "scratches", md,
    Column("user_id", BigInteger, primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("kind", String(8)),
    Column("value", String(32)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

prizes = Table(
    "prizes", md,
    Column("id", String(32), primary_key=True),
    Column("name_ru", String(128)),
    Column("name_uz", String(128)),
    Column("cost", Integer),
    Column("stock", Integer),
    Column("icon", String(32)),
    Column("sort", Integer, default=0),
    Column("active", Boolean, nullable=False, default=True),
)

claims = Table(
    "claims", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, index=True),
    Column("prize_id", String(32)),
    Column("pickup", String(8)),
    Column("status", String(16), default="pending"),      # pending → delivered
    Column("source", String(16), default="shop"),          # shop | scratch | code
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

survey_answers = Table(
    "survey_answers", md,
    Column("user_id", BigInteger, primary_key=True),
    Column("answers", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)

DEFAULT_PRIZES = [
    ("stick", "Стикерпак «Черноголовка»", "«Chernogolovka» stikerpaki", 50, 240, "stick", 1),
    ("open", "Брелок-открывашка", "Ochqich-brelok", 150, 86, "open", 2),
    ("moj", "Мохито Экстра 0,5 л в подарок", "Sovg‘aga Moxito Ekstra 0,5 l", 200, 400, "img:mx_wild", 3),
    ("tee", "Футболка с логотипом", "Logotipli futbolka", 400, 34, "tee", 4),
    ("cola", "Упаковка Cola 0,5 л (12 штук)", "Cola 0,5 l qadoq (12 dona)", 600, 20, "img:cola", 5),
    ("bag", "Термосумка для пикника", "Piknik uchun termosumka", 800, 9, "bag", 6),
]


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(md.create_all)
        n = (await conn.execute(select(func.count()).select_from(prizes))).scalar()
        if not n:
            await conn.execute(insert(prizes), [
                dict(id=i, name_ru=r, name_uz=u, cost=c, stock=s, icon=ic, sort=o, active=True)
                for i, r, u, c, s, ic, o in DEFAULT_PRIZES])
