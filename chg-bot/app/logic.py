"""Правила клуба. Все начисления и лимиты считаются только здесь, на сервере."""
import random
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update, insert, func, and_

from .config import TZ, DEMO_CODES
from .db import users, codes, ledger, plays, scratches, prizes, claims, survey_answers

TIERS = [("bronze", 0, 10), ("silver", 300, 12), ("gold", 1000, 15)]
GAMES = ("caps", "catch", "ninja", "mix")
PAID_ATTEMPTS = 3          # баллы только за первые 3 попытки в каждой игре в день
GAME_DAY_CAP = 40          # не больше 40 баллов за игры в день
GAME_MAX_POINTS = 15       # не больше 15 баллов за одну игру
CONV = {"caps": lambda s: s // 100, "catch": lambda s: s // 40, "ninja": lambda s: s // 30, "mix": lambda s: s * 2}
# Защита от подмены результата: потолок очков и минимальная длительность партии
MAX_SCORE = {"caps": 8000, "catch": 4000, "ninja": 3000, "mix": 40}
MIN_SECONDS = {"caps": 28, "catch": 28, "ninja": 3, "mix": 4}
FAIL_LIMIT, FAIL_WINDOW, LOCK_FOR = 5, timedelta(minutes=10), timedelta(minutes=10)
SURVEY_POINTS, BIRTHDAY_POINTS = 30, 50


def now():
    return datetime.now(TZ)


def today():
    return now().strftime("%Y-%m-%d")


def per_code(lifetime: int) -> int:
    per = TIERS[0][2]
    for _, mn, p in TIERS:
        if lifetime >= mn:
            per = p
    return per


def norm_code(raw: str) -> str:
    v = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if v.startswith("CHG") and len(v) > 8:
        v = v[3:]
    return v[:8]


def fmt_code(c: str) -> str:
    return f"CHG-{c[:4]}-{c[4:]}"


def masked(first: str | None, last: str | None) -> str:
    first = (first or "Гость").strip()[:20]
    return f"{first} {last.strip()[0]}." if last and last.strip() else first


def aw(dt):
    """SQLite отдаёт даты без часового пояса — считаем их ташкентскими."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt


def ts(dt) -> int:
    return int(aw(dt).timestamp() * 1000) if dt else 0


async def add_points(conn, uid: int, delta: int, reason: str, ref: str = ""):
    if not delta:
        return
    vals = {"points": users.c.points + delta}
    if delta > 0:
        vals["lifetime"] = users.c.lifetime + delta
    await conn.execute(update(users).where(users.c.id == uid).values(**vals))
    await conn.execute(insert(ledger).values(user_id=uid, delta=delta, reason=reason, ref=ref, created_at=now()))


async def upsert_user(conn, tg: dict) -> dict:
    uid = int(tg["id"])
    row = (await conn.execute(select(users).where(users.c.id == uid))).mappings().first()
    fields = dict(first_name=(tg.get("first_name") or "")[:128], last_name=(tg.get("last_name") or "")[:128],
                  username=(tg.get("username") or None), last_seen=now())
    if row:
        await conn.execute(update(users).where(users.c.id == uid).values(**fields, blocked_bot=False))
    else:
        lang = "uz" if (tg.get("language_code") or "").startswith("uz") else "ru"
        await conn.execute(insert(users).values(id=uid, lang=lang, points=0, lifetime=0, created_at=now(), **fields))
    return dict((await conn.execute(select(users).where(users.c.id == uid))).mappings().first())


async def profile(conn, uid: int) -> dict:
    u = (await conn.execute(select(users).where(users.c.id == uid))).mappings().first()
    d = today()
    cs = (await conn.execute(select(codes).where(codes.c.used_by == uid).order_by(codes.c.used_at.desc()).limit(20))).mappings().all()
    cl = (await conn.execute(select(claims).where(claims.c.user_id == uid).order_by(claims.c.id.desc()).limit(20))).mappings().all()
    pl = (await conn.execute(select(plays.c.game, func.count(), func.coalesce(func.sum(plays.c.points), 0))
                              .where(and_(plays.c.user_id == uid, plays.c.day == d)).group_by(plays.c.game))).all()
    rec = (await conn.execute(select(plays.c.game, func.max(plays.c.score))
                              .where(and_(plays.c.user_id == uid, plays.c.flag.is_(None))).group_by(plays.c.game))).all()
    sc = (await conn.execute(select(scratches).where(and_(scratches.c.user_id == uid, scratches.c.day == d)))).mappings().first()
    cat = (await conn.execute(select(prizes.c.id, prizes.c.stock, prizes.c.cost))).all()
    scratched = None
    if sc:
        scratched = {"kind": "pts", "n": int(sc["value"])} if sc["kind"] == "pts" else {"kind": "prize", "id": sc["value"]}
    return {
        "name": u["first_name"] or "Гость", "lang": u["lang"], "points": u["points"], "lifetime": u["lifetime"],
        "birthday": u["birthday"], "survey": u["survey_done"], "onboarded": u["onboarded"],
        "lockUntil": ts(u["lock_until"]) if u["lock_until"] and aw(u["lock_until"]) > now() else 0,
        "codes": [{"code": fmt_code(c["code"]), "pts": c["points"] or 0, "ts": ts(c["used_at"]), "gift": c["gift_prize"]} for c in cs],
        "prizes": [{"id": c["prize_id"], "ts": ts(c["created_at"]), "pick": c["pickup"], "status": c["status"]} for c in cl],
        "day": {"date": d, "plays": {g: n for g, n, _ in pl}, "earned": int(sum(p for _, _, p in pl)), "scratched": scratched},
        "records": {g: (s or 0) for g, s in rec},
        "catalog": [{"id": i, "stock": s, "cost": c} for i, s, c in cat],
    }


def _demo_valid(c: str) -> bool:
    return sum(ord(ch) for ch in c) % 3 == 0


async def redeem(conn, uid: int, raw: str) -> dict:
    u = (await conn.execute(select(users).where(users.c.id == uid))).mappings().first()
    t = now()
    if u["banned"]:
        return {"ok": False, "err": "invalid"}
    if u["lock_until"] and aw(u["lock_until"]) > t:
        return {"ok": False, "err": "locked", "m": max(1, int((aw(u["lock_until"]) - t).total_seconds() // 60) + 1)}
    c = norm_code(raw)
    if len(c) != 8:
        return {"ok": False, "err": "format"}
    row = (await conn.execute(select(codes).where(codes.c.code == c))).mappings().first()
    if row is None and DEMO_CODES and _demo_valid(c):
        gift = "stick" if sum(ord(ch) for ch in c) % 9 == 0 else None
        await conn.execute(insert(codes).values(code=c, gift_prize=gift))
        row = {"code": c, "used_by": None, "gift_prize": gift}
    if row is None:
        fs = aw(u["fails_since"])
        since = fs if fs and t - fs < FAIL_WINDOW else t
        fails = (u["fails"] if since != t else 0) + 1
        vals = {"fails": fails, "fails_since": since}
        if fails >= FAIL_LIMIT:
            vals = {"fails": 0, "fails_since": None, "lock_until": t + LOCK_FOR}
        await conn.execute(update(users).where(users.c.id == uid).values(**vals))
        return {"ok": False, "err": "invalid"}
    if row["used_by"] is not None:
        return {"ok": False, "err": "used"}
    pts = per_code(u["lifetime"])
    # атомарно: код засчитывается только если его ещё никто не занял
    res = await conn.execute(update(codes).where(and_(codes.c.code == c, codes.c.used_by.is_(None)))
                             .values(used_by=uid, used_at=t, points=pts))
    if res.rowcount != 1:
        return {"ok": False, "err": "used"}
    await conn.execute(update(users).where(users.c.id == uid).values(fails=0, fails_since=None))
    await add_points(conn, uid, pts, "code", c)
    gift = None
    if row["gift_prize"]:
        p = (await conn.execute(select(prizes).where(prizes.c.id == row["gift_prize"]))).mappings().first()
        if p:
            await conn.execute(insert(claims).values(user_id=uid, prize_id=p["id"], pickup=pickup_code(), status="pending", source="code", created_at=now()))
            gift = p["id"]
    return {"ok": True, "pts": pts, "gift": gift}


def pickup_code() -> str:
    return str(secrets.randbelow(900000) + 100000)


async def game_start(conn, uid: int, game: str) -> dict:
    d = today()
    n = (await conn.execute(select(func.count()).select_from(plays)
                            .where(and_(plays.c.user_id == uid, plays.c.game == game, plays.c.day == d)))).scalar() or 0
    res = await conn.execute(insert(plays).values(user_id=uid, game=game, day=d, attempt=n + 1, started_at=now(), points=0))
    return {"play": res.inserted_primary_key[0], "attempt": n + 1}


async def game_submit(conn, uid: int, play_id: int, score: int) -> dict:
    p = (await conn.execute(select(plays).where(and_(plays.c.id == play_id, plays.c.user_id == uid)))).mappings().first()
    if not p or p["finished_at"] is not None:
        return {"pts": 0, "reason": "invalid", "rec": False}
    t = now()
    game, score = p["game"], max(0, int(score))
    elapsed = (t - aw(p["started_at"])).total_seconds()
    flag = None
    if score > MAX_SCORE[game]:
        flag = "score_too_high"
    elif score > 0 and elapsed < MIN_SECONDS[game]:
        flag = "too_fast"
    elif game == "mix" and score > elapsed / 2.5:
        flag = "too_fast"
    prev = (await conn.execute(select(func.max(plays.c.score))
                               .where(and_(plays.c.user_id == uid, plays.c.game == game, plays.c.flag.is_(None))))).scalar() or 0
    pts, reason = 0, ""
    if flag:
        reason = "invalid"
    elif p["attempt"] > PAID_ATTEMPTS:
        reason = "practice"
    else:
        earned = (await conn.execute(select(func.coalesce(func.sum(plays.c.points), 0))
                                     .where(and_(plays.c.user_id == uid, plays.c.day == p["day"])))).scalar() or 0
        if earned >= GAME_DAY_CAP:
            reason = "cap"
        else:
            pts = min(max(0, min(GAME_MAX_POINTS, CONV[game](score))), GAME_DAY_CAP - earned)
            if not pts:
                reason = "zero"
    await conn.execute(update(plays).where(plays.c.id == play_id).values(finished_at=t, score=score, points=pts, flag=flag))
    if pts:
        await add_points(conn, uid, pts, "game", f"{game}:{play_id}")
    return {"pts": pts, "reason": reason, "rec": (not flag) and score > prev and score > 0}


async def scratch(conn, uid: int) -> dict:
    d = today()
    row = (await conn.execute(select(scratches).where(and_(scratches.c.user_id == uid, scratches.c.day == d)))).mappings().first()
    if row:
        return {"kind": row["kind"], "n": int(row["value"])} if row["kind"] == "pts" else {"kind": "prize", "id": row["value"]}
    r = random.random()
    stick = (await conn.execute(select(prizes.c.stock).where(prizes.c.id == "stick"))).scalar() or 0
    if r < 0.03 and stick > 0:
        res = await conn.execute(update(prizes).where(and_(prizes.c.id == "stick", prizes.c.stock > 0)).values(stock=prizes.c.stock - 1))
        if res.rowcount == 1:
            await conn.execute(insert(scratches).values(user_id=uid, day=d, kind="prize", value="stick", created_at=now()))
            await conn.execute(insert(claims).values(user_id=uid, prize_id="stick", pickup=pickup_code(), status="pending", source="scratch", created_at=now()))
            return {"kind": "prize", "id": "stick"}
    n = 30 if r < 0.2 else 15 if r < 0.55 else 5
    await conn.execute(insert(scratches).values(user_id=uid, day=d, kind="pts", value=str(n), created_at=now()))
    await add_points(conn, uid, n, "scratch", d)
    return {"kind": "pts", "n": n}


async def claim_prize(conn, uid: int, prize_id: str) -> dict:
    p = (await conn.execute(select(prizes).where(and_(prizes.c.id == prize_id, prizes.c.active.is_(True))))).mappings().first()
    if not p:
        return {"ok": False, "err": "nf"}
    res = await conn.execute(update(users).where(and_(users.c.id == uid, users.c.points >= p["cost"], users.c.banned.is_(False)))
                             .values(points=users.c.points - p["cost"]))
    if res.rowcount != 1:
        return {"ok": False, "err": "points"}
    res = await conn.execute(update(prizes).where(and_(prizes.c.id == prize_id, prizes.c.stock > 0)).values(stock=prizes.c.stock - 1))
    if res.rowcount != 1:
        raise OutOfStock()
    pick = pickup_code()
    r = await conn.execute(insert(claims).values(user_id=uid, prize_id=prize_id, pickup=pick, status="pending", source="shop", created_at=now()))
    await conn.execute(insert(ledger).values(user_id=uid, delta=-p["cost"], reason="prize", ref=prize_id, created_at=now()))
    return {"ok": True, "claim": r.inserted_primary_key[0], "pickup": pick, "prize": dict(p)}


class OutOfStock(Exception):
    pass


async def leaderboard(conn, uid: int, period: str) -> dict:
    q = select(codes.c.used_by, func.count().label("n")).where(codes.c.used_by.is_not(None))
    if period == "week":
        q = q.where(codes.c.used_at >= now() - timedelta(days=7))
    sub = q.group_by(codes.c.used_by).subquery()
    rows = (await conn.execute(select(users.c.id, users.c.first_name, users.c.last_name, sub.c.n)
                               .join(sub, sub.c.used_by == users.c.id).where(users.c.banned.is_(False))
                               .order_by(sub.c.n.desc(), users.c.id).limit(15))).all()
    mine = (await conn.execute(select(sub.c.n).where(sub.c.used_by == uid))).scalar() or 0
    above = (await conn.execute(select(func.count()).select_from(sub.join(users, users.c.id == sub.c.used_by))
                                .where(and_(sub.c.n > mine, users.c.banned.is_(False))))).scalar() or 0
    ids = [i for i, _, _, _ in rows]
    pos = ids.index(uid) + 1 if uid in ids else above + 1
    return {"rows": [{"n": masked(f, l), "s": n, "me": i == uid} for i, f, l, n in rows], "mine": {"s": mine, "pos": pos}}


async def winners_today(conn, lang: str) -> list:
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (await conn.execute(select(users.c.first_name, users.c.last_name, prizes.c.name_ru, prizes.c.name_uz, claims.c.created_at)
                               .join(users, users.c.id == claims.c.user_id).join(prizes, prizes.c.id == claims.c.prize_id)
                               .where(and_(claims.c.created_at >= start, users.c.banned.is_(False)))
                               .order_by(claims.c.id.desc()).limit(3))).all()
    return [{"n": masked(f, l), "p": (pu if lang == "uz" else pr), "h": aw(c).astimezone(TZ).hour, "m": aw(c).astimezone(TZ).minute}
            for f, l, pr, pu, c in rows]


async def save_survey(conn, uid: int, answers) -> bool:
    u = (await conn.execute(select(users.c.survey_done).where(users.c.id == uid))).scalar()
    if u:
        return False
    import json
    await conn.execute(insert(survey_answers).values(user_id=uid, answers=json.dumps(answers, ensure_ascii=False), created_at=now()))
    await conn.execute(update(users).where(users.c.id == uid).values(survey_done=True))
    await add_points(conn, uid, SURVEY_POINTS, "survey")
    return True


async def save_birthday(conn, uid: int, date: str) -> bool:
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    if not (1920 <= d.year <= now().year - 5):
        return False
    res = await conn.execute(update(users).where(and_(users.c.id == uid, users.c.birthday.is_(None))).values(birthday=date))
    if res.rowcount != 1:
        return False
    await add_points(conn, uid, BIRTHDAY_POINTS, "birthday")
    return True
