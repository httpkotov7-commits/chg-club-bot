"""Telegram-бот: вход в клуб, описание, админ-команды, рассылки, заявки на призы, импорт."""
import asyncio
import csv
import io
import logging

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
                           MenuButtonWebApp, BotCommand)
from sqlalchemy import select, update, insert, func, and_

from . import logic, texts
from .config import BOT_TOKEN, WEBAPP_URL, ADMIN_IDS
from .db import engine, users, codes, claims, prizes, ledger

log = logging.getLogger("bot")
router = Router()
PENDING: dict[int, dict] = {}   # админ → подготовленная рассылка


def make_bot() -> Bot:
    return Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def club_kb(lang: str) -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=texts.OPEN_BTN[lang], web_app=WebAppInfo(url=WEBAPP_URL))]])


def is_admin(m) -> bool:
    return m.from_user and m.from_user.id in ADMIN_IDS


# ---------- пользователи ----------
@router.message(CommandStart())
async def start(m: Message):
    async with engine.begin() as conn:
        u = await logic.upsert_user(conn, m.from_user.model_dump())
    await m.answer(texts.WELCOME[u["lang"]], reply_markup=club_kb(u["lang"]))


# ---------- админка ----------
@router.message(Command("help"), F.func(is_admin))
async def help_(m: Message):
    await m.answer(
        "<b>Админ-команды</b>\n"
        "/stats — статистика клуба\n"
        "/claims — заявки на призы (отметить выданными)\n"
        "/announce — анонс «превращаемся в клуб» всем пользователям\n"
        "/broadcast — ответьте этой командой на любое сообщение (текст, фото, видео) — оно уйдёт всем с кнопкой клуба\n"
        "/import_users — пришлите CSV с подписью /import_users (telegram_id[,points[,first_name]])\n"
        "/import_codes — пришлите CSV с подписью /import_codes (код[,id_подарка])\n"
        "/ban ID и /unban ID — скрыть/вернуть пользователя (накрутки)\n"
        "/prizes — остатки призов, /stock ID число — задать остаток")


@router.message(Command("stats"), F.func(is_admin))
async def stats(m: Message):
    async with engine.begin() as conn:
        q = lambda s: conn.execute(s)
        total = (await q(select(func.count()).select_from(users))).scalar()
        active = (await q(select(func.count()).select_from(users).where(users.c.last_seen >= logic.now() - logic.timedelta(days=7)))).scalar()
        blocked = (await q(select(func.count()).select_from(users).where(users.c.blocked_bot.is_(True)))).scalar()
        used = (await q(select(func.count()).select_from(codes).where(codes.c.used_by.is_not(None)))).scalar()
        free = (await q(select(func.count()).select_from(codes).where(codes.c.used_by.is_(None)))).scalar()
        pend = (await q(select(func.count()).select_from(claims).where(claims.c.status == "pending"))).scalar()
        pts = (await q(select(func.coalesce(func.sum(users.c.points), 0)))).scalar()
        bday = (await q(select(func.count()).select_from(users).where(users.c.birthday.is_not(None)))).scalar()
    await m.answer(f"👥 Пользователей: <b>{total}</b> (активны за 7 дней: {active}, заблокировали бота: {blocked})\n"
                   f"🧢 Кодов активировано: <b>{used}</b>, свободных в базе: {free}\n"
                   f"🎁 Заявок на призы ждут выдачи: <b>{pend}</b>\n"
                   f"💰 Баллов на руках: {pts}\n🎂 Указали дату рождения: {bday}")


@router.message(Command("claims"), F.func(is_admin))
async def list_claims(m: Message):
    async with engine.begin() as conn:
        rows = (await conn.execute(select(claims.c.id, claims.c.pickup, claims.c.source, prizes.c.name_ru, users.c.first_name,
                                          users.c.last_name, users.c.username, users.c.id.label("uid"))
                                   .join(prizes, prizes.c.id == claims.c.prize_id).join(users, users.c.id == claims.c.user_id)
                                   .where(claims.c.status == "pending").order_by(claims.c.id).limit(20))).all()
    if not rows:
        return await m.answer("Нет заявок, ожидающих выдачи.")
    for r in rows:
        who = f"{r.first_name} {r.last_name or ''}".strip() + (f" @{r.username}" if r.username else "")
        await m.answer(f"#{r.id} · {r.name_ru}\n{who} (id {r.uid})\nКод получения: <b>{r.pickup}</b> · источник: {r.source}",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Выдан", callback_data=f"dlv:{r.id}")]]))


@router.callback_query(F.data.startswith("dlv:"))
async def delivered(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer()
    cid = int(c.data.split(":")[1])
    async with engine.begin() as conn:
        res = await conn.execute(update(claims).where(and_(claims.c.id == cid, claims.c.status == "pending")).values(status="delivered"))
        row = (await conn.execute(select(claims.c.user_id, claims.c.pickup, prizes.c.name_ru, prizes.c.name_uz, users.c.lang)
                                  .join(prizes, prizes.c.id == claims.c.prize_id).join(users, users.c.id == claims.c.user_id)
                                  .where(claims.c.id == cid))).first()
    if res.rowcount and row:
        try:
            await bot.send_message(row.user_id, texts.DELIVERED[row.lang].format(name=row.name_uz if row.lang == "uz" else row.name_ru, pick=row.pickup))
        except Exception:  # noqa
            pass
    await c.message.edit_text(c.message.html_text + "\n\n✅ <b>Выдан</b>")
    await c.answer("Отмечено")


@router.message(Command("prizes"), F.func(is_admin))
async def prize_list(m: Message):
    async with engine.begin() as conn:
        rows = (await conn.execute(select(prizes).order_by(prizes.c.sort))).mappings().all()
    await m.answer("\n".join(f"<code>{p['id']}</code> · {p['name_ru']} · {p['cost']} б. · остаток {p['stock']}" for p in rows))


@router.message(Command("stock"), F.func(is_admin))
async def set_stock(m: Message, command: CommandObject):
    try:
        pid, n = (command.args or "").split()
        n = int(n)
    except ValueError:
        return await m.answer("Формат: /stock ID число")
    async with engine.begin() as conn:
        res = await conn.execute(update(prizes).where(prizes.c.id == pid).values(stock=n))
    await m.answer("Готово" if res.rowcount else "Нет такого приза")


@router.message(Command("ban", "unban"), F.func(is_admin))
async def ban(m: Message, command: CommandObject):
    try:
        uid = int(command.args)
    except (TypeError, ValueError):
        return await m.answer("Формат: /ban ID")
    async with engine.begin() as conn:
        res = await conn.execute(update(users).where(users.c.id == uid).values(banned=command.command == "ban"))
    await m.answer("Готово" if res.rowcount else "Пользователь не найден")


# ---------- рассылки ----------
async def audience() -> list[tuple[int, str]]:
    async with engine.begin() as conn:
        return [(r.id, r.lang) for r in (await conn.execute(select(users.c.id, users.c.lang).where(users.c.blocked_bot.is_(False))))]


def confirm_kb(n: int):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🚀 Отправить ({n})", callback_data="bc:go"),
                                                  InlineKeyboardButton(text="Отмена", callback_data="bc:no")]])


@router.message(Command("announce"), F.func(is_admin))
async def announce(m: Message):
    PENDING[m.from_user.id] = {"kind": "announce"}
    await m.answer(texts.ANNOUNCE["ru"], reply_markup=club_kb("ru"))
    await m.answer("Так увидят пользователи (узбекоязычные — на узбекском). Отправить всем?", reply_markup=confirm_kb(len(await audience())))


@router.message(Command("broadcast"), F.func(is_admin))
async def broadcast(m: Message):
    src = m.reply_to_message
    if not src:
        return await m.answer("Ответьте командой /broadcast на сообщение, которое нужно разослать (можно видео с подписью).")
    PENDING[m.from_user.id] = {"kind": "copy", "chat": src.chat.id, "msg": src.message_id}
    await m.answer("Разослать это сообщение всем пользователям с кнопкой «Открыть клуб»?", reply_markup=confirm_kb(len(await audience())))


@router.callback_query(F.data.startswith("bc:"))
async def bc_go(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer()
    job = PENDING.pop(c.from_user.id, None)
    if c.data == "bc:no" or not job:
        await c.message.edit_text("Рассылка отменена.")
        return await c.answer()
    await c.message.edit_text("Рассылка запущена… Пришлю итог.")
    await c.answer()
    asyncio.create_task(run_broadcast(bot, c.from_user.id, job))


async def run_broadcast(bot: Bot, admin: int, job: dict):
    sent = failed = blocked = 0
    for uid, lang in await audience():
        for _ in range(3):
            try:
                if job["kind"] == "announce":
                    await bot.send_message(uid, texts.ANNOUNCE[lang], reply_markup=club_kb(lang))
                else:
                    await bot.copy_message(uid, job["chat"], job["msg"], reply_markup=club_kb(lang))
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                blocked += 1
                async with engine.begin() as conn:
                    await conn.execute(update(users).where(users.c.id == uid).values(blocked_bot=True))
                break
            except TelegramBadRequest:
                failed += 1
                break
        await asyncio.sleep(0.04)   # ~25 сообщений в секунду, ниже лимита Telegram
    await bot.send_message(admin, f"Рассылка завершена: доставлено {sent}, заблокировали бота {blocked}, ошибок {failed}.")


# ---------- импорт ----------
async def read_csv(bot: Bot, m: Message) -> list[list[str]]:
    f = await bot.download(m.document)
    text = f.read().decode("utf-8-sig", errors="ignore")
    return [r for r in csv.reader(io.StringIO(text), delimiter=";" if text.count(";") > text.count(",") else ",") if r and r[0].strip()]


@router.message(F.document, F.caption.startswith("/import_users"), F.func(is_admin))
async def import_users(m: Message, bot: Bot):
    rows = await read_csv(bot, m)
    added = updated = 0
    async with engine.begin() as conn:
        for r in rows:
            if not r[0].strip().lstrip("-").isdigit():
                continue  # заголовок
            uid = int(r[0])
            pts = int(r[1]) if len(r) > 1 and r[1].strip().lstrip("-").isdigit() else 0
            name = r[2].strip()[:128] if len(r) > 2 else ""
            exists = (await conn.execute(select(users.c.id).where(users.c.id == uid))).first()
            if not exists:
                await conn.execute(insert(users).values(id=uid, first_name=name, lang="ru", points=0, lifetime=0, created_at=logic.now()))
                added += 1
            else:
                updated += 1
            migrated = (await conn.execute(select(ledger.c.id).where(and_(ledger.c.user_id == uid, ledger.c.reason == "migration")))).first()
            if pts and not migrated:
                await logic.add_points(conn, uid, pts, "migration")
    await m.answer(f"Импорт пользователей: новых {added}, уже были {updated}. Баллы перенесены один раз на пользователя.")


@router.message(F.document, F.caption.startswith("/import_codes"), F.func(is_admin))
async def import_codes(m: Message, bot: Bot):
    rows = await read_csv(bot, m)
    batch = {}
    for r in rows:
        c = logic.norm_code(r[0])
        if len(c) == 8:
            batch[c] = (r[1].strip() or None) if len(r) > 1 else None
    added = 0
    keys = list(batch)
    async with engine.begin() as conn:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            have = {x for (x,) in (await conn.execute(select(codes.c.code).where(codes.c.code.in_(chunk))))}
            new = [{"code": k, "gift_prize": batch[k]} for k in chunk if k not in have]
            if new:
                await conn.execute(insert(codes), new)
                added += len(new)
    await m.answer(f"Импорт кодов: добавлено {added}, пропущено дублей {len(keys) - added}.")


# ---------- запуск ----------
async def setup_bot(bot: Bot):
    for lang in ("ru", "uz"):
        code = None if lang == "ru" else "uz"
        try:
            await bot.set_my_description(texts.DESCRIPTION[lang], language_code=code)
            await bot.set_my_short_description(texts.SHORT[lang], language_code=code)
        except Exception as e:  # noqa
            log.warning("description: %s", e)
    await bot.set_my_commands([BotCommand(command="start", description="Открыть клуб")])
    if WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text=texts.MENU_BTN["ru"], web_app=WebAppInfo(url=WEBAPP_URL)))


def make_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
