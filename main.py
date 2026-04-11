import discord
from discord.ext import commands
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger("SquadBot")

load_dotenv()

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True 
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
        self.db = self.mongo_client.squad_clan_db 
        
        self.config = {
            "STEAM_API_KEY": os.getenv("STEAM_API_KEY"),
            "CLAN_ROLE_ID": int(os.getenv("CLAN_ROLE_ID")),
            "ADMIN_ROLE_IDS": [int(role_id) for role_id in os.getenv("ADMIN_ROLE_IDS").split(",")]
        }

    async def setup_hook(self):
        # Загрузка модулей
        await self.load_extension("cogs.xp_system")
        await self.load_extension("cogs.tracker")
        
        # Установка глобального обработчика ошибок для Slash-команд
        self.tree.on_error = self.on_app_command_error
        
        # Синхронизация команд
        await self.tree.sync()
        logger.info("Бот успешно запущен! Команды синхронизированы.")

    # Глобальный обработчик ошибок
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return

        logger.error(f"Ошибка в команде {interaction.command.name if interaction.command else 'Unknown'}: {error}", exc_info=error)
        
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Произошла непредвиденная ошибка при выполнении команды. Повтори попытку позже.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Произошла непредвиденная ошибка при выполнении команды. Повтори попытку позже.", ephemeral=True)
        except Exception:
            pass

    async def close(self):
        self.mongo_client.close()
        await super().close()

if __name__ == "__main__":
    bot = MyBot()
    bot.run(os.getenv("DISCORD_TOKEN"), log_handler=None)