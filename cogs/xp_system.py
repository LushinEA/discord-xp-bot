import discord
from discord.ext import commands
from discord import app_commands
from utils import is_bot_admin
import logging
import asyncio

logger = logging.getLogger("SquadBot")

EMBED_DESCRIPTION_LIMIT = 4096


class XPSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.achievements = self.bot.db.achievements
        self.ranks = self.bot.db.ranks

    async def update_member_rank(self, member: discord.Member):
        """Считает опыт по ролям-ачивкам и СИНХРОНИЗИРУЕТ звания"""
        clan_role_id = self.bot.config["CLAN_ROLE_ID"]
        ranks_list = await self.ranks.find().sort("required_xp", -1).to_list(length=None)
        all_rank_ids = [rank["_id"] for rank in ranks_list]

        if clan_role_id not in [r.id for r in member.roles]:
            roles_to_remove = [r for r in member.roles if r.id in all_rank_ids]
            if roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove)
                    logger.info(f"[-] Сняты звания у {member.display_name} (нет клановой роли)")
                except discord.Forbidden:
                    logger.error("Ошибка прав! Роль бота должна быть ВЫШЕ ролей званий!")
            return

        try:
            achievements_cursor = await self.achievements.find().to_list(length=None)
            achievement_dict = {doc["_id"]: doc["xp"] for doc in achievements_cursor}

            total_xp = sum(achievement_dict[role.id] for role in member.roles if role.id in achievement_dict)

            target_rank_id = None
            for rank in ranks_list:
                if total_xp >= rank["required_xp"]:
                    target_rank_id = rank["_id"]
                    break

            roles_to_add, roles_to_remove = [], []

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

    async def build_profile_embed(self, member: discord.Member, guild: discord.Guild) -> discord.Embed:
        """Общая логика построения профиля опыта для админов и юзеров"""
        achievements_cursor = await self.achievements.find().to_list(length=None)
        achievement_dict = {doc["_id"]: doc["xp"] for doc in achievements_cursor}
        total_xp = sum(achievement_dict[role.id] for role in member.roles if role.id in achievement_dict)

        ranks_cursor = await self.ranks.find().sort("required_xp", 1).to_list(length=None)
        current_rank = None
        next_rank = None

        for rank in ranks_cursor:
            if total_xp >= rank["required_xp"]:
                current_rank = guild.get_role(rank["_id"])
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
            next_role = guild.get_role(next_rank["_id"])
            next_role_name = next_role.name if next_role else "???"
            req_xp = next_rank["required_xp"]

            bar_length = 8
            progress = int((total_xp / req_xp) * bar_length) if req_xp > 0 else bar_length
            progress = max(0, min(bar_length, progress))
            bar = "🟩" * progress + "⬛" * (bar_length - progress)

            embed.add_field(
                name=f"📈 До звания «{next_role_name}»",
                value=f"{total_xp} / {req_xp} XP\n{bar}",
                inline=False
            )
        else:
            embed.add_field(name="📈 Прогресс", value="Достигнуто максимальное звание! 👑", inline=False)

        return embed

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles != after.roles:
            await self.update_member_rank(after)

    # ==========================================
    # КОМАНДЫ: ПРОСМОТР ТАБЛИЦ (для всего клана)
    # ==========================================

    @app_commands.command(name="ranks_list", description="Показать таблицу всех воинских званий и требуемого опыта")
    async def ranks_list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        ranks_cursor = await self.ranks.find().sort("required_xp", 1).to_list(length=None)

        if not ranks_cursor:
            return await interaction.followup.send("В базе нет ни одного звания.")

        embed = discord.Embed(
            title="🎖️ Таблица воинских званий",
            description="Список всех доступных воинских званий и опыт, необходимый для их получения.",
            color=discord.Color.gold()
        )

        lines = []
        for i, rank in enumerate(ranks_cursor):
            role = interaction.guild.get_role(rank["_id"])
            role_display = role.mention if role else f"`ID: {rank['_id']}`"
            lines.append(f"**{rank['required_xp']} XP** — {role_display}")

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Всего званий: {len(ranks_cursor)}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="achievements_list", description="Показать все доступные достижения (или по конкретной категории)")
    @app_commands.describe(category="Выберите категорию для фильтрации (необязательно)")
    async def achievements_list(self, interaction: discord.Interaction, category: str = None):
        await interaction.response.defer()

        query = {"category": category} if category else {}
        achievements_cursor = await self.achievements.find(query).sort("xp", 1).to_list(length=None)

        if not achievements_cursor:
            if category:
                return await interaction.followup.send(f"В категории **{category}** пока нет достижений.")
            return await interaction.followup.send("В базе нет ни одного достижения.")

        categories: dict[str, list] = {}
        for ach in achievements_cursor:
            cat = ach.get("category") or "Без категории"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(ach)

        total_achievements = len(achievements_cursor)
        
        if category:
            header = f"📋 Категория: **{category}** | Достижений: **{total_achievements}**"
        else:
            header = f"📋 Всего достижений: **{total_achievements}**, категорий: **{len(categories)}**"
            
        await interaction.followup.send(content=header)

        for cat_name, achs in categories.items():
            lines = []
            for ach in achs:
                role = interaction.guild.get_role(ach["_id"])
                role_display = role.mention if role else f"`ID: {ach['_id']}`"
                desc = ach.get("description", "—")
                lines.append(f"`{ach['xp']:>3} XP` {role_display} — {desc}")

            chunks = []
            current_chunk: list[str] = []
            current_len = 0

            for line in lines:
                line_len = len(line) + 1
                if current_len + line_len > EMBED_DESCRIPTION_LIMIT:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_len = line_len
                else:
                    current_chunk.append(line)
                    current_len += line_len

            if current_chunk:
                chunks.append("\n".join(current_chunk))

            for part_idx, chunk in enumerate(chunks):
                title = f"🏅 {cat_name}"
                if len(chunks) > 1:
                    title += f" (часть {part_idx + 1}/{len(chunks)})"

                embed = discord.Embed(
                    title=title,
                    description=chunk,
                    color=discord.Color.blue()
                )
                if part_idx == len(chunks) - 1:
                    embed.set_footer(text=f"Достижений: {len(achs)}")

                await interaction.followup.send(embed=embed)

    @achievements_list.autocomplete("category")
    async def category_autocomplete(self, interaction: discord.Interaction, current: str):
        unique_categories = await self.achievements.distinct("category")
        
        cleaned_categories = [c if c else "Без категории" for c in unique_categories]
        
        choices = [
            app_commands.Choice(name=cat, value=cat)
            for cat in cleaned_categories
            if current.lower() in cat.lower()
        ]
        
        return choices[:25]

    # ==========================================
    # УПРАВЛЕНИЕ АЧИВКАМИ
    # ==========================================

    @app_commands.command(name="add_achievement", description="[АДМИН] Добавить роль как ачивку")
    @is_bot_admin()
    async def add_achievement(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        xp: int,
        description: str,
        category: str = "Без категории"
    ):
        await interaction.response.defer(ephemeral=True)

        if await self.achievements.find_one({"_id": role.id}):
            return await interaction.followup.send(f"❌ Роль {role.mention} **уже добавлена** как ачивка. Используйте `/edit_achievement` для изменения.")
        if await self.ranks.find_one({"_id": role.id}):
            return await interaction.followup.send(f"❌ Ошибка логики! Роль {role.mention} уже используется как **звание**.")

        await self.achievements.insert_one({
            "_id": role.id,
            "xp": xp,
            "description": description,
            "category": category
        })
        logger.info(f"Админ {interaction.user} добавил ачивку {role.name} ({xp} XP, категория: {category}).")
        await interaction.followup.send(
            f"✅ Ачивка {role.mention} сохранена!\n"
            f"⚡ Даёт **{xp} XP**\n"
            f"📂 Категория: **{category}**\n"
            f"📝 Описание: {description}"
        )

    @app_commands.command(name="edit_achievement", description="[АДМИН] Изменить XP, описание или категорию ачивки")
    @is_bot_admin()
    async def edit_achievement(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        new_xp: int,
        new_description: str,
        new_category: str = None
    ):
        await interaction.response.defer(ephemeral=True)

        existing = await self.achievements.find_one({"_id": role.id})
        if not existing:
            return await interaction.followup.send(f"❌ Роль {role.mention} не найдена в списке ачивок. Сначала добавьте её через `/add_achievement`.")

        update_data = {"xp": new_xp, "description": new_description}
        if new_category is not None:
            update_data["category"] = new_category

        await self.achievements.update_one({"_id": role.id}, {"$set": update_data})
        logger.info(f"Админ {interaction.user} изменил ачивку {role.name} на {new_xp} XP.")

        category_info = f"\n📂 Категория: **{new_category}**" if new_category else ""
        await interaction.followup.send(
            f"✏️ Ачивка {role.mention} успешно обновлена!\n"
            f"⚡ Теперь даёт **{new_xp} XP**{category_info}"
        )

    @app_commands.command(name="set_achievement_category", description="[АДМИН] Установить категорию для существующей ачивки")
    @is_bot_admin()
    async def set_achievement_category(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        category: str
    ):
        """Позволяет задать/изменить категорию ачивки без смены других параметров."""
        await interaction.response.defer(ephemeral=True)

        existing = await self.achievements.find_one({"_id": role.id})
        if not existing:
            return await interaction.followup.send(f"❌ Роль {role.mention} не найдена в списке ачивок.")

        old_category = existing.get("category", "Без категории")
        await self.achievements.update_one({"_id": role.id}, {"$set": {"category": category}})

        logger.info(f"Админ {interaction.user} изменил категорию ачивки {role.name}: '{old_category}' → '{category}'.")
        await interaction.followup.send(
            f"✏️ Категория ачивки {role.mention} обновлена:\n"
            f"**{old_category}** → **{category}**"
        )

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

    @app_commands.command(name="sync_ranks", description="[АДМИН] Принудительно обновить звания у всего клана")
    @is_bot_admin()
    async def sync_ranks(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clan_role_id = self.bot.config["CLAN_ROLE_ID"]

        updated_count = 0
        for member in interaction.guild.members:
            if member.bot:
                continue
            if clan_role_id in [r.id for r in member.roles]:
                await self.update_member_rank(member)
                updated_count += 1
                if updated_count % 10 == 0:
                    await asyncio.sleep(0.2)

        await interaction.followup.send(f"✅ Синхронизация завершена! Проверено и обновлено бойцов: **{updated_count}**.")

    @app_commands.command(name="check_profile", description="[АДМИН] Посмотреть профиль XP и звания конкретного бойца")
    @is_bot_admin()
    async def check_profile(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()

        if self.bot.config["CLAN_ROLE_ID"] not in [r.id for r in member.roles]:
            return await interaction.followup.send(f"⚠️ У {member.mention} нет клановой роли — профиль недоступен.")

        await self.update_member_rank(member)
        embed = await self.build_profile_embed(member, interaction.guild)
        embed.set_footer(text=f"Запрошено администратором: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    # ==========================================
    # ПРОФИЛЬ (Пользовательская команда)
    # ==========================================

    @app_commands.command(name="profile", description="Посмотреть свой опыт и прогресс")
    async def profile(self, interaction: discord.Interaction):
        await interaction.response.defer()
        member = interaction.user

        if self.bot.config["CLAN_ROLE_ID"] not in [r.id for r in member.roles]:
            return await interaction.followup.send("Профиль доступен только участникам клана.")

        await self.update_member_rank(member)
        embed = await self.build_profile_embed(member, interaction.guild)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(XPSystem(bot))