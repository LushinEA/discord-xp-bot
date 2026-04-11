import discord
from discord.ext import commands
from discord import app_commands
from utils import is_bot_admin
import logging
import asyncio

logger = logging.getLogger("SquadBot")

class XPSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.achievements = self.bot.db.achievements 
        self.ranks = self.bot.db.ranks

    async def update_member_rank(self, member: discord.Member):
        """Считает опыт по ролям-ачивкам и СИНХРОНИЗИРУЕТ звания"""
        clan_role_id = self.bot.config["CLAN_ROLE_ID"]
        if clan_role_id not in [r.id for r in member.roles]:
            return

        try:
            achievements_cursor = await self.achievements.find().to_list(length=None)
            achievement_dict = {doc["_id"]: doc["xp"] for doc in achievements_cursor}

            total_xp = sum(achievement_dict[role.id] for role in member.roles if role.id in achievement_dict)
            ranks_list = await self.ranks.find().sort("required_xp", -1).to_list(length=None)
            
            target_rank_id = None
            for rank in ranks_list:
                if total_xp >= rank["required_xp"]:
                    target_rank_id = rank["_id"]
                    break

            roles_to_add, roles_to_remove = [], []
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
            logger.error("Ошибка прав! Роль бота должна быть ВЫШЕ ролей званий в настройках сервера!")
        except Exception as e:
            logger.error(f"Ошибка при пересчете опыта у {member.display_name}: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles != after.roles:
            await self.update_member_rank(after)
    
    # ==========================================
    # УПРАВЛЕНИЕ АЧИВКАМИ
    # ==========================================

    @app_commands.command(name="add_achievement", description="[АДМИН] Добавить роль как ачивку")
    @is_bot_admin()
    async def add_achievement(self, interaction: discord.Interaction, role: discord.Role, xp: int, description: str):
        await interaction.response.defer(ephemeral=True)
        
        if await self.achievements.find_one({"_id": role.id}):
            return await interaction.followup.send(f"❌ Роль {role.mention} **уже добавлена** как ачивка. Используйте `/edit_achievement` для изменения.")
        if await self.ranks.find_one({"_id": role.id}):
            return await interaction.followup.send(f"❌ Ошибка логики! Роль {role.mention} уже используется как **звание**.")

        await self.achievements.insert_one({"_id": role.id, "xp": xp, "description": description})
        logger.info(f"Админ {interaction.user} добавил ачивку {role.name} ({xp} XP).")
        await interaction.followup.send(f"✅ Ачивка {role.mention} сохранена! Дает **{xp} XP**.\n📝 Описание: {description}")

    @app_commands.command(name="edit_achievement", description="[АДМИН] Изменить XP или описание у существующей ачивки")
    @is_bot_admin()
    async def edit_achievement(self, interaction: discord.Interaction, role: discord.Role, new_xp: int, new_description: str):
        await interaction.response.defer(ephemeral=True)
        
        if not await self.achievements.find_one({"_id": role.id}):
            return await interaction.followup.send(f"❌ Роль {role.mention} не найдена в списке ачивок. Сначала добавьте её через `/add_achievement`.")

        await self.achievements.update_one({"_id": role.id}, {"$set": {"xp": new_xp, "description": new_description}})
        logger.info(f"Админ {interaction.user} изменил ачивку {role.name} на {new_xp} XP.")
        await interaction.followup.send(f"✏️ Ачивка {role.mention} успешно обновлена! Теперь дает **{new_xp} XP**.")

    @app_commands.command(name="remove_achievement", description="[АДМИН] Удалить ачивку из БД и забрать её у всех пользователей")
    @is_bot_admin()
    async def remove_achievement(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        result = await self.achievements.delete_one({"_id": role.id})
        
        if result.deleted_count > 0:
            removed_count = 0
            for member in role.members:
                try:
                    await member.remove_roles(role)
                    removed_count += 1
                    await asyncio.sleep(0.5) 
                except discord.Forbidden:
                    logger.error(f"Не удалось снять роль {role.name} с {member.display_name} (Forbidden)")
            
            logger.info(f"Админ {interaction.user} удалил ачивку {role.name}. Снято с {removed_count} чел.")
            await interaction.followup.send(f"🗑️ Роль {role.mention} удалена из системы ачивок и физически снята с **{removed_count}** пользователей.")
        else:
            await interaction.followup.send(f"⚠️ Эта роль не найдена в базе ачивок.")

    # ==========================================
    # УПРАВЛЕНИЕ ЗВАНИЯМИ (РАНГАМИ)
    # ==========================================

    @app_commands.command(name="add_rank", description="[АДМИН] Добавить роль как звание")
    @is_bot_admin()
    async def add_rank(self, interaction: discord.Interaction, role: discord.Role, required_xp: int):
        await interaction.response.defer(ephemeral=True)
        
        if await self.ranks.find_one({"_id": role.id}):
            return await interaction.followup.send(f"❌ Роль {role.mention} **уже добавлена** как звание. Используйте `/edit_rank` для изменения.")
        if await self.achievements.find_one({"_id": role.id}):
            return await interaction.followup.send(f"❌ Ошибка логики! Роль {role.mention} уже используется как **ачивка**.")

        await self.ranks.insert_one({"_id": role.id, "required_xp": required_xp})
        logger.info(f"Админ {interaction.user} добавил звание {role.name} ({required_xp} XP).")
        await interaction.followup.send(f"✅ Звание {role.mention} сохранено! Требуется **{required_xp} XP**.")

    @app_commands.command(name="edit_rank", description="[АДМИН] Изменить порог XP для существующего звания")
    @is_bot_admin()
    async def edit_rank(self, interaction: discord.Interaction, role: discord.Role, new_required_xp: int):
        await interaction.response.defer(ephemeral=True)
        
        if not await self.ranks.find_one({"_id": role.id}):
            return await interaction.followup.send(f"❌ Роль {role.mention} не найдена в списке званий. Сначала добавьте её через `/add_rank`.")

        await self.ranks.update_one({"_id": role.id}, {"$set": {"required_xp": new_required_xp}})
        logger.info(f"Админ {interaction.user} изменил порог звания {role.name} на {new_required_xp} XP.")
        await interaction.followup.send(f"✏️ Звание {role.mention} успешно обновлено! Теперь требуется **{new_required_xp} XP**.")

    @app_commands.command(name="remove_rank", description="[АДМИН] Удалить звание из БД и забрать его у всех пользователей")
    @is_bot_admin()
    async def remove_rank(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        result = await self.ranks.delete_one({"_id": role.id})
        
        if result.deleted_count > 0:
            removed_count = 0
            for member in role.members:
                try:
                    await member.remove_roles(role)
                    removed_count += 1
                    await asyncio.sleep(0.5) 
                except discord.Forbidden:
                    logger.error(f"Не удалось снять звание {role.name} с {member.display_name} (Forbidden)")

            logger.info(f"Админ {interaction.user} удалил звание {role.name}. Снято с {removed_count} чел.")
            await interaction.followup.send(f"🗑️ Роль {role.mention} удалена из списка званий и физически снята с **{removed_count}** пользователей.")
        else:
            await interaction.followup.send(f"⚠️ Эта роль не найдена в базе званий.")
    
    # ==========================================
    # ПРОФИЛЬ (Пользовательская команда)
    # ==========================================

    @app_commands.command(name="profile", description="Посмотреть свой опыт и прогресс")
    async def profile(self, interaction: discord.Interaction):
        await interaction.response.defer()
        member = interaction.user
        
        if self.bot.config["CLAN_ROLE_ID"] not in [r.id for r in member.roles]:
            return await interaction.followup.send("Профиль доступен только участникам клана.")
            
        achievements_cursor = await self.achievements.find().to_list(length=None)
        achievement_dict = {doc["_id"]: doc["xp"] for doc in achievements_cursor}
        total_xp = sum(achievement_dict[role.id] for role in member.roles if role.id in achievement_dict)
        
        ranks_cursor = await self.ranks.find().sort("required_xp", 1).to_list(length=None)
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
            next_role_name = next_role.name if next_role else "???"
            req_xp = next_rank["required_xp"]
            
            progress = int((total_xp / req_xp) * 10) if req_xp > 0 else 10
            progress = max(0, min(10, progress))
            bar = "🟩" * progress + "⬛" * (10 - progress)
            
            embed.add_field(
                name=f"📈 До звания «{next_role_name}»", 
                value=f"{total_xp} / {req_xp} XP\n{bar}", 
                inline=False
            )
        else:
            embed.add_field(name="📈 Прогресс", value="Вы достигли максимального звания! 👑", inline=False)

        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(XPSystem(bot))