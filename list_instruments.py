import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crypto.encryption import decrypt
from db.connection import Database
import okx.PublicData as PublicData


def main():
    db = Database()
    # берём первого юзера (у тебя seed.py создал user_id=1)
    row = db.execute("SELECT okx_api_key, okx_secret_key, okx_passphrase FROM user_settings WHERE user_id = 1")
    if not row:
        print("user_id=1 не найден в user_settings")
        return

    # для public-инструментов ключи не нужны, но просто покажем, что всё работает
    public = PublicData.PublicAPI("", "", "", False, "1")  # "1" = demo

    result = public.get_instruments(instType="SWAP")
    code = result.get("code")
    if code != "0":
        print("Ошибка OKX:", result)
        return

    data = result.get("data", [])
    print(f"Всего SWAP инструментов: {len(data)}")
    print("Первые 20 USDT-SWAP:")
    count = 0
    for inst in data:
        if inst.get("settleCcy") != "USDT":
            continue
        print(
            f"{inst['instId']:15} ctVal={inst['ctVal']:>8} "
            f"tickSz={inst['tickSz']:>8} lotSz={inst['lotSz']:>8} state={inst['state']}"
        )
        count += 1
        if count >= 146:
            break


if __name__ == "__main__":
    main()