import os
import logging
import threading
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands
from fastapi import FastAPI
import uvicorn
from handlers import SET, register_handlers, split_by_lines

load_dotenv()

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

MAX_LENGTH = 2000

@bot.event
async def on_ready():
    logging.info(f"✅ Bot logged in as {bot.user} (ID: {bot.user.id})")

register_handlers(bot)


app = FastAPI()

# Funkcja pomocnicza realizująca operacje na Discordzie
async def send_discord_response(channel, response):
    # Pokazujemy potok pisania na kanale
    await channel.typing()
    
    if len(response) <= MAX_LENGTH:
        # NAPRAWIONE: message.channel.send zamiast message.send
        await channel.send(response)
    else:
        chunks = split_by_lines(response)
        await channel.send(chunks[0]) 
        for chunk in chunks[1:]:
            await channel.send(chunk)

@app.post("/alert")
def add_alert(response: str):
    """Endpoint przyjmujący odpowiedź od agenta i zlecający wysyłkę na Discorda"""
    if not SET:
        return {"status": "error", "message": "Brak wiadomości w historii (SET jest pusty)"}

    # Bierzemy pierwszą (lub ostatnią) wiadomość z historii, żeby wyciągnąć z niej kanał
    for channel in SET:
        print("\n\nOstatnia wiadomość (użyty kanał):", channel.name)
    
        # Przekazujemy wykonanie bezpiecznie do pętli zdarzeń bota
        asyncio.run_coroutine_threadsafe(
            send_discord_response(channel, response), 
            bot.loop
        )

    return {
        "status": "running",
        "bot_connected": bot.is_ready(),
        "bot_user": str(bot.user) if bot.user else None
    }


def run_uvicorn():
    uvicorn.run(app, host="127.0.0.1", port=8146, log_level="info")


def main():
    logging.info("🚀 Starting API Server thread...")
    api_thread = threading.Thread(target=run_uvicorn, daemon=True)
    api_thread.start()

    logging.info("🚀 Starting Discord bot...")
    bot.run(os.getenv("DISCORD_BOT_TOKEN"))

if __name__ == "__main__":
    main()