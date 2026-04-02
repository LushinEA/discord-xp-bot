import discord
from discord.ext import commands
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from dotenv import load_dotenv


# НАСТРОЙКА ЛОГИРОВАНИЯ
# Создаем логгер, который пишет и в консоль, и в файл bot.log
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("XP_Bot")


# ЗАГРУЗКА ПЕРЕМЕННЫХ И ИНИЦИАЛИЗАЦИЯ
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

intents = discord.Intents.default()
intents.members = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        
        self.mongo_client = AsyncIOMotorClient(MONGO_URI)
        self.db = self.mongo_client.discord_xp_bot
        self.achievements = self.db.achievements
        self.ranks = self.db.ranks

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Бот успешно запущен! Подключение к MongoDB установлено. Команды синхронизированы.")

bot = MyBot()


# ЛОГИКА ПЕРЕСЧЕТА ОПЫТА И ЗВАНИЙ
async def update_member_rank(member: discord.Member):
    """Считает опыт по ролям-ачивкам и СИНХРОНИЗИРУЕТ звания"""
    try:
        achievements_cursor = await bot.achievements.find().to_list(length=None)
        achievement_dict = {doc["_id"]: doc["xp"] for doc in achievements_cursor}

        total_xp = sum(achievement_dict[role.id] for role in member.roles if role.id in achievement_dict)

        ranks_list = await bot.ranks.find().sort("required_xp", -1).to_list(length=None)
        
        target_rank_id = None
        for rank in ranks_list:
            if total_xp >= rank["required_xp"]:
                target_rank_id = rank["_id"]
                break

        roles_to_add = []
        roles_to_remove = []
        all_rank_ids = [rank["_id"] for rank in ranks_list]

        for role in member.roles:
            if role.id in all_rank_ids and role.id != target_rank_id:
                roles_to_remove.append(role)

        if target_rank_id:
            target_role = member.guild.get_role(target_rank_id)
            if target_role and target_role not in member.roles:
                roles_to_add.append(target_role)

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
            logger.info(f"[-] Сняты неактуальные звания у пользователя: {member.display_name}")
            
        if roles_to_add:
            await member.add_roles(*roles_to_add)
            logger.info(f"[+] Выдано новое звание для пользователя: {member.display_name}")
            
    except discord.Forbidden:
        logger.error(f"Ошибка прав! Роль бота должна быть ВЫШЕ ролей званий в настройках сервера!")
    except Exception as e:
        logger.error(f"Ошибка при пересчете опыта у {member.display_name}: {e}", exc_info=True)


# СОБЫТИЯ
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.roles != after.roles:
        await update_member_rank(after)


# АДМИН-ПАНЕЛЬ
@bot.tree.command(name="add_achievement", description="[АДМИН] Добавить/обновить роль как ачивку")
@app_commands.checks.has_permissions(administrator=True)
async def add_achievement(interaction: discord.Interaction, role: discord.Role, xp: int, description: str):
    await interaction.response.defer(ephemeral=True)
    try:
        await bot.achievements.update_one(
            {"_id": role.id}, {"$set": {"xp": xp, "description": description}}, upsert=True
        )
        logger.info(f"Админ {interaction.user} добавил ачивку {role.name} ({xp} XP).")
        await interaction.followup.send(f"✅ Ачивка {role.mention} сохранена! Дает **{xp} XP**.\n📝 Описание: {description}")
    except Exception as e:
        logger.error(f"Ошибка БД при добавлении ачивки: {e}", exc_info=True)
        await interaction.followup.send("❌ Произошла ошибка при сохранении в базу данных.")

@bot.tree.command(name="remove_achievement", description="[АДМИН] Удалить роль из списка ачивок")
@app_commands.checks.has_permissions(administrator=True)
async def remove_achievement(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    try:
        result = await bot.achievements.delete_one({"_id": role.id})
        if result.deleted_count > 0:
            logger.info(f"Админ {interaction.user} удалил ачивку {role.name}.")
            await interaction.followup.send(f"🗑️ Роль {role.mention} удалена из системы ачивок.")
        else:
            await interaction.followup.send(f"⚠️ Эта роль и так не являлась ачивкой.")
    except Exception as e:
        logger.error(f"Ошибка БД при удалении ачивки: {e}", exc_info=True)
        await interaction.followup.send("❌ Ошибка при удалении из базы данных.")

@bot.tree.command(name="add_rank", description="[АДМИН] Добавить/обновить роль как звание")
@app_commands.checks.has_permissions(administrator=True)
async def add_rank(interaction: discord.Interaction, role: discord.Role, required_xp: int):
    await interaction.response.defer(ephemeral=True)
    try:
        await bot.ranks.update_one(
            {"_id": role.id}, {"$set": {"required_xp": required_xp}}, upsert=True
        )
        logger.info(f"Админ {interaction.user} добавил звание {role.name} ({required_xp} XP).")
        await interaction.followup.send(f"✅ Звание {role.mention} сохранено! Требуется **{required_xp} XP**.")
    except Exception as e:
        logger.error(f"Ошибка БД при добавлении звания: {e}", exc_info=True)
        await interaction.followup.send("❌ Произошла ошибка при сохранении в базу данных.")

@bot.tree.command(name="remove_rank", description="[АДМИН] Удалить роль из списка званий")
@app_commands.checks.has_permissions(administrator=True)
async def remove_rank(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    try:
        result = await bot.ranks.delete_one({"_id": role.id})
        if result.deleted_count > 0:
            logger.info(f"Админ {interaction.user} удалил звание {role.name}.")
            await interaction.followup.send(f"🗑️ Роль {role.mention} удалена из списка званий.")
        else:
            await interaction.followup.send(f"⚠️ Эта роль и так не являлась званием.")
    except Exception as e:
        logger.error(f"Ошибка БД при удалении звания: {e}", exc_info=True)
        await interaction.followup.send("❌ Ошибка при удалении из базы данных.")


# ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ
@bot.tree.command(name="profile", description="Посмотреть свой опыт и прогресс до следующего звания")
async def profile(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        member = interaction.user
        
        achievements_cursor = await bot.achievements.find().to_list(length=None)
        achievement_dict = {doc["_id"]: doc["xp"] for doc in achievements_cursor}
        total_xp = sum(achievement_dict[role.id] for role in member.roles if role.id in achievement_dict)
        
        ranks_cursor = await bot.ranks.find().sort("required_xp", 1).to_list(length=None)

        current_rank = None
        next_rank = None
        
        for rank in ranks_cursor:
            if total_xp >= rank["required_xp"]:
                current_rank = interaction.guild.get_role(rank["_id"])
            elif total_xp < rank["required_xp"] and next_rank is None:
                next_rank = rank
                break

        embed = discord.Embed(title=f"Профиль: {member.display_name}", color=discord.Color.gold())
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        
        rank_mention = current_rank.mention if current_rank else "Отсутствует"
        embed.add_field(name="🏆 Текущее звание", value=rank_mention, inline=False)
        embed.add_field(name="⚡ Накопленный опыт", value=f"**{total_xp} XP**", inline=False)
        
        if next_rank:
            next_role = interaction.guild.get_role(next_rank["_id"])
            next_role_name = next_role.name if next_role else "Неизвестное звание"
            req_xp = next_rank["required_xp"]
            
            progress = int((total_xp / req_xp) * 10) if req_xp > 0 else 10
            bar = "🟩" * progress + "⬛" * (10 - progress)
            
            embed.add_field(name=f"📈 До звания «{next_role_name}»", 
                            value=f"{total_xp} / {req_xp} XP\n{bar}", inline=False)
        else:
            embed.add_field(name="📈 Прогресс", value="Вы достигли максимального звания! 👑", inline=False)

        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Ошибка в профиле у {interaction.user}: {e}", exc_info=True)
        await interaction.followup.send("❌ Произошла ошибка при загрузке профиля. Попробуйте позже.")


if __name__ == "__main__":
    discord.utils.setup_logging(level=logging.INFO, root=False)
    bot.run(TOKEN, log_handler=None)