"""Сгенерировать коды для крышек (если коды печатаете вы, а не производство).

python -m scripts.gen_codes 10000 codes.csv [--gift stick:20]
Алфавит без похожих символов (нет O/0, I/1). Каждые --gift N кодов получают подарок.
"""
import secrets
import sys

ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    out = sys.argv[2] if len(sys.argv) > 2 else "codes.csv"
    gift, every = None, 0
    if "--gift" in sys.argv:
        gift, every = sys.argv[sys.argv.index("--gift") + 1].split(":")
        every = int(every)
    seen = set()
    with open(out, "w", encoding="utf-8") as f:
        f.write("code,gift\n")
        while len(seen) < n:
            c = "".join(secrets.choice(ALPHA) for _ in range(8))
            if c in seen:
                continue
            seen.add(c)
            g = gift if every and len(seen) % every == 0 else ""
            f.write(f"CHG-{c[:4]}-{c[4:]},{g}\n")
    print(f"{n} кодов → {out}")


if __name__ == "__main__":
    main()
