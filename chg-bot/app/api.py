"""HTTP-сервер: раздаёт мини-апп и обрабатывает его запросы (/api/*)."""
import logging
import os

from aiohttp import web
from sqlalchemy import update

from . import logic
from .auth import check_init_data
from .config import BOT_TOKEN, INITDATA_MAX_AGE, WEBAPP_DIR, ADMIN_IDS
from .db import engine, users

log = logging.getLogger("api")
BOT = web.AppKey("bot", object)


@web.middleware
async def auth_mw(request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    tg = check_init_data(request.headers.get("X-Init-Data", ""), BOT_TOKEN, INITDATA_MAX_AGE)
    if not tg:
        return web.json_response({"error": "unauthorized"}, status=401)
    async with engine.begin() as conn:
        u = await logic.upsert_user(conn, tg)
    request["uid"], request["tg"], request["user"] = int(tg["id"]), tg, u
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:  # noqa
        log.exception("api error %s", request.path)
        return web.json_response({"error": "server"}, status=500)


async def body(request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa
        return {}


async def with_me(conn, uid, payload: dict | None = None):
    out = dict(payload or {})
    out["me"] = await logic.profile(conn, uid)
    return web.json_response(out)


async def me(request):
    async with engine.begin() as conn:
        return await with_me(conn, request["uid"])


async def set_lang(request):
    lang = (await body(request)).get("lang")
    if lang in ("ru", "uz"):
        async with engine.begin() as conn:
            await conn.execute(update(users).where(users.c.id == request["uid"]).values(lang=lang))
    return web.json_response({"ok": True})


async def onboarded(request):
    async with engine.begin() as conn:
        await conn.execute(update(users).where(users.c.id == request["uid"]).values(onboarded=True))
    return web.json_response({"ok": True})


async def redeem(request):
    code = str((await body(request)).get("code", ""))
    async with engine.begin() as conn:
        r = await logic.redeem(conn, request["uid"], code)
    async with engine.begin() as conn:  # отдельная транзакция: неудачная попытка тоже должна сохраниться
        return await with_me(conn, request["uid"], r)


async def game_start(request):
    g = (await body(request)).get("game")
    if g not in logic.GAMES:
        return web.json_response({"error": "game"}, status=400)
    async with engine.begin() as conn:
        r = await logic.game_start(conn, request["uid"], g)
        return await with_me(conn, request["uid"], r)


async def game_submit(request):
    b = await body(request)
    try:
        play, score = int(b.get("play")), int(b.get("score", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad"}, status=400)
    async with engine.begin() as conn:
        r = await logic.game_submit(conn, request["uid"], play, score)
        return await with_me(conn, request["uid"], r)


async def scratch(request):
    async with engine.begin() as conn:
        r = await logic.scratch(conn, request["uid"])
        return await with_me(conn, request["uid"], {"prize": r})


async def claim(request):
    pid = str((await body(request)).get("prize", ""))
    try:
        async with engine.begin() as conn:
            r = await logic.claim_prize(conn, request["uid"], pid)
    except logic.OutOfStock:
        r = {"ok": False, "err": "stock"}
    if r.get("ok"):
        await notify_claim(request, r)
        r = {"ok": True, "pickup": r["pickup"]}
    async with engine.begin() as conn:
        return await with_me(conn, request["uid"], r)


async def notify_claim(request, r):
    bot = request.app.get(BOT)
    if not bot:
        return
    u, p = request["user"], r["prize"]
    uz = u.get("lang") == "uz"
    try:
        await bot.send_message(u["id"], (f"🎁 «{p['name_uz']}» uchun so‘rov qabul qilindi. Olish kodi: <b>{r['pickup']}</b>.\nSovg‘a tayyor bo‘lganda shu yerga yozamiz."
                                         if uz else f"🎁 Заявка на «{p['name_ru']}» принята. Код получения: <b>{r['pickup']}</b>.\nНапишем сюда, когда приз будет готов к выдаче."))
    except Exception:  # noqa
        pass
    who = f"{u['first_name']} {u['last_name'] or ''}".strip() + (f" @{u['username']}" if u.get("username") else "")
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, f"🧾 Заявка #{r['claim']}: {p['name_ru']}\n{who} (id {u['id']})\nКод получения: {r['pickup']}\n/claims — список заявок")
        except Exception:  # noqa
            pass


async def leaderboard(request):
    period = request.query.get("period", "week")
    async with engine.begin() as conn:
        return web.json_response(await logic.leaderboard(conn, request["uid"], "all" if period == "all" else "week"))


async def winners(request):
    async with engine.begin() as conn:
        return web.json_response({"rows": await logic.winners_today(conn, request["user"]["lang"])})


async def survey(request):
    async with engine.begin() as conn:
        ok = await logic.save_survey(conn, request["uid"], (await body(request)).get("answers", []))
        return await with_me(conn, request["uid"], {"ok": ok})


async def birthday(request):
    async with engine.begin() as conn:
        ok = await logic.save_birthday(conn, request["uid"], (await body(request)).get("date"))
        return await with_me(conn, request["uid"], {"ok": ok})


async def index(request):
    return web.FileResponse(os.path.join(WEBAPP_DIR, "index.html"), headers={"Cache-Control": "no-cache"})


async def health(request):
    return web.json_response({"ok": True})


def build_app(bot=None) -> web.Application:
    app = web.Application(middlewares=[auth_mw], client_max_size=2 * 1024 * 1024)
    app[BOT] = bot
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", health)
    r.add_get("/api/me", me)
    r.add_post("/api/lang", set_lang)
    r.add_post("/api/onboarded", onboarded)
    r.add_post("/api/redeem", redeem)
    r.add_post("/api/game/start", game_start)
    r.add_post("/api/game/submit", game_submit)
    r.add_post("/api/scratch", scratch)
    r.add_post("/api/claim", claim)
    r.add_get("/api/leaderboard", leaderboard)
    r.add_get("/api/winners", winners)
    r.add_post("/api/survey", survey)
    r.add_post("/api/birthday", birthday)
    for d in ("img", "fonts"):
        p = os.path.join(WEBAPP_DIR, d)
        if os.path.isdir(p):
            r.add_static(f"/{d}/", p)
    return app
