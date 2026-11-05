"""Импорт из консоли (то же, что /import_codes и /import_users в боте).

python -m scripts.import_csv codes codes.csv      # код[,id_подарка]
python -m scripts.import_csv users users.csv      # telegram_id[,баллы[,имя]]
"""
import asyncio
import csv
import sys

from sqlalchemy import select, insert, and_

from app import logic
from app.db import engine, init_db, codes, users, ledger


async def main(kind: str, path: str):
    await init_db()
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    rows = [r for r in csv.reader(text.splitlines(), delimiter=";" if text.count(";") > text.count(",") else ",") if r and r[0].strip()]
    n = 0
    async with engine.begin() as conn:
        if kind == "codes":
            batch = {logic.norm_code(r[0]): ((r[1].strip() or None) if len(r) > 1 else None) for r in rows if len(logic.norm_code(r[0])) == 8}
            keys = list(batch)
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                have = {x for (x,) in (await conn.execute(select(codes.c.code).where(codes.c.code.in_(chunk))))}
                new = [{"code": k, "gift_prize": batch[k]} for k in chunk if k not in have]
                if new:
                    await conn.execute(insert(codes), new)
                    n += len(new)
        else:
            for r in rows:
                if not r[0].strip().lstrip("-").isdigit():
                    continue
                uid = int(r[0])
                pts = int(r[1]) if len(r) > 1 and r[1].strip().lstrip("-").isdigit() else 0
                if not (await conn.execute(select(users.c.id).where(users.c.id == uid))).first():
                    await conn.execute(insert(users).values(id=uid, first_name=(r[2].strip() if len(r) > 2 else ""), lang="ru",
                                                            points=0, lifetime=0, created_at=logic.now()))
                    n += 1
                if pts and not (await conn.execute(select(ledger.c.id).where(and_(ledger.c.user_id == uid, ledger.c.reason == "migration")))).first():
                    await logic.add_points(conn, uid, pts, "migration")
    print(f"готово: добавлено {n}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
