# login.py
import os
from dotenv import load_dotenv
from telethon import TelegramClient

# 1) Cargar .env desde el mismo directorio
load_dotenv()  # si tu .env está aquí, esto lo toma

# 2) Depuración: imprime lo que lee del .env
print("DEBUG API_ID =", os.getenv("API_ID"))
print("DEBUG API_HASH =", os.getenv("API_HASH"))
print("DEBUG PHONE =", os.getenv("PHONE_NUMBER"))

# 3) Credenciales
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE = os.getenv("PHONE_NUMBER", "")

# 4) Cliente con nombre de sesión que usa tu main.py
client = TelegramClient("forwarder", API_ID, API_HASH)

async def main():
    print("👉 Te pedirá NÚMERO y CÓDIGO de Telegram (si la sesión no existe)")
    await client.start(phone=PHONE)
    me = await client.get_me()
    print("✅ Sesión guardada. is_bot =", bool(getattr(me, "bot", False)))

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
