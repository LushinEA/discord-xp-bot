import discord
from discord import app_commands

def is_bot_admin():
    async def predicate(interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Эта команда доступна только на сервере.", ephemeral=True)
            return False

        admin_roles = interaction.client.config["ADMIN_ROLE_IDS"]
        
        if any(role.id in admin_roles for role in interaction.user.roles):
            return True
            
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды.", ephemeral=True)
        return False
        
    return app_commands.check(predicate)