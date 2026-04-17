import discord
from discord.ext import tasks, commands
from discord import app_commands
from pymongo import UpdateOne
from utils import is_bot_admin
import datetime
import re
import logging
import io
import asyncio

logger = logging.getLogger("SquadBot")
SEED_THRESHOLD = 50
TRAINING_SERVER_MARKER = "[FREE] Zone"
TRAINING_SERVER_NAME = "[FREE] Zone - Training Server"

# Названия карт Squad - такие записи не являются реальным сервером и игнорируются
SQUAD_MAP_NAMES = {
    "anvil", "al basrah", "belaya", "black coast", "chora", "fallujah",
    "fool's road", "fools road", "gorodok", "harju", "jensen's range",
    "jensens range", "kamdesh highlands", "kamdesh", "kohat toi", "kohat",
    "kokan", "lashkar valley", "lashkar", "logar valley", "logar",
    "manicouagan", "manic-5", "mestia", "mutaha", "nanisivik", "narva",
    "op first light", "pacific proving grounds", "sanxian islands", "sanxian",
    "skorpo", "squamish valley", "squamish", "sumari bala", "sumari",
    "tallil outskirts", "tallil", "yehorivka", "yamalia", "munduz",
}

PERIOD_CHOICES = [
    app_commands.Choice(name="За всё время", value="all"),
    app_commands.Choice(name="1 день", value="1"),
    app_commands.Choice(name="3 дня", value="3"),
    app_commands.Choice(name="7 дней", value="7"),
    app_commands.Choice(name="14 дней", value="14"),
    app_commands.Choice(name="1 месяц", value="30"),
    app_commands.Choice(name="6 месяцев", value="180"),
    app_commands.Choice(name="1 год", value="365"),
]

class ActivityTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.activity = self.bot.db.activity
        self.daily_activity = self.bot.db.daily_activity
        self.users = self.bot.db.users

    async def cog_load(self):
        if not self.track_activity.is_running():
            self.track_activity.start()

    async def cog_unload(self):
        self.track_activity.cancel()

    def parse_squad_info(self, activity):
        """
        Возвращает (server_name, is_seed, is_training) или (None, False, False) если запись не валидна
        (Main Menu, название карты вместо сервера).
        """
        raw_text = activity.large_image_text or activity.details or ""
        server_name = raw_text.split(" on ", 1)[1].strip() if " on " in raw_text else raw_text.strip() or "Main Menu"

        # Пункт 1: игнорируем Main Menu
        if server_name == "Main Menu":
            return None, False, False

        # Пункт 2: игнорируем записи, которые являются названием карты, а не сервера
        if server_name.lower() in SQUAD_MAP_NAMES:
            return None, False, False

        if TRAINING_SERVER_MARKER in server_name:
            return TRAINING_SERVER_NAME, False, True

        is_seed = False
        details = activity.details or ""
        match = re.search(r'\((\d+)/\d+\)', details)
        if match and int(match.group(1)) < SEED_THRESHOLD:
            is_seed = True

        return server_name.replace(".", "_").replace("$", ""), is_seed, False

    @tasks.loop(minutes=1)
    async def track_activity(self):
        now = datetime.datetime.utcnow()
        today_str = now.strftime('%Y-%m-%d')
        active_users = {}
        processed_discord_ids = set()
        clan_role_id = self.bot.config["CLAN_ROLE_ID"]

        try:
            users_cursor = await self.users.find().to_list(length=None)
            discord_to_steam = {doc["discord_id"]: doc["steam_id"] for doc in users_cursor}

            count = 0
            for guild in self.bot.guilds:
                for member in guild.members:
                    count += 1
                    if count % 50 == 0:
                        await asyncio.sleep(0.01)

                    if member.bot or member.id in processed_discord_ids:
                        continue
                    processed_discord_ids.add(member.id)

                    if clan_role_id not in [r.id for r in member.roles]:
                        continue

                    steam_id = discord_to_steam.get(member.id)
                    if not steam_id:
                        continue

                    current_squad = next(
                        (act for act in member.activities if act.name == "Squad" and isinstance(act, discord.Activity)),
                        None
                    )

                    if current_squad:
                        srv_name, is_seed, is_training = self.parse_squad_info(current_squad)
                        if srv_name is not None:
                            active_users[steam_id] = {
                                "server": srv_name,
                                "is_seed": is_seed,
                                "is_training": is_training
                            }

            if active_users:
                global_ops = []
                daily_ops = []

                for steam_id, info in active_users.items():
                    if info["is_training"]:
                        prefix = "training"
                    elif info["is_seed"]:
                        prefix = "seeding"
                    else:
                        prefix = "battle"

                    global_ops.append(UpdateOne(
                        {"_id": str(steam_id)},
                        {
                            "$set": {"last_seen": now},
                            "$inc": {
                                f"{prefix}_servers.{info['server']}": 1,
                                f"total_{prefix}_minutes": 1,
                                "total_minutes": 1
                            }
                        },
                        upsert=True
                    ))

                    daily_ops.append(UpdateOne(
                        {"steam_id": str(steam_id), "date": today_str},
                        {
                            "$inc": {
                                f"{prefix}_servers.{info['server']}": 1,
                                f"total_{prefix}_minutes": 1,
                                "total_minutes": 1
                            }
                        },
                        upsert=True
                    ))

                if global_ops:
                    await self.activity.bulk_write(global_ops, ordered=False)
                if daily_ops:
                    await self.daily_activity.bulk_write(daily_ops, ordered=False)

        except Exception as e:
            logger.error(f"Ошибка в трекере активности: {e}")

    @track_activity.before_loop
    async def before_track_activity(self):
        await self.bot.wait_until_ready()

    # ==========================================
    # ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ СБОРА ДАННЫХ
    # ==========================================
    async def fetch_user_stats(self, steam_id: str, period: str):
        if period == "all":
            return await self.activity.find_one({"_id": steam_id})

        target_date = (datetime.datetime.utcnow() - datetime.timedelta(days=int(period))).strftime('%Y-%m-%d')
        cursor = self.daily_activity.find({"steam_id": steam_id, "date": {"$gte": target_date}})

        aggregated_data = {
            "total_battle_minutes": 0,
            "total_seeding_minutes": 0,
            "total_training_minutes": 0,
            "total_minutes": 0,
            "battle_servers": {},
            "seeding_servers": {},
            "training_servers": {}
        }

        has_data = False
        async for doc in cursor:
            has_data = True
            aggregated_data["total_minutes"] += doc.get("total_minutes", 0)
            aggregated_data["total_battle_minutes"] += doc.get("total_battle_minutes", 0)
            aggregated_data["total_seeding_minutes"] += doc.get("total_seeding_minutes", 0)
            aggregated_data["total_training_minutes"] += doc.get("total_training_minutes", 0)

            for srv, mins in doc.get("battle_servers", {}).items():
                aggregated_data["battle_servers"][srv] = aggregated_data["battle_servers"].get(srv, 0) + mins
            for srv, mins in doc.get("seeding_servers", {}).items():
                aggregated_data["seeding_servers"][srv] = aggregated_data["seeding_servers"].get(srv, 0) + mins
            for srv, mins in doc.get("training_servers", {}).items():
                aggregated_data["training_servers"][srv] = aggregated_data["training_servers"].get(srv, 0) + mins

        return aggregated_data if has_data else None

    # ==========================================
    # ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: аддитивный мёрж серверов
    # ==========================================
    def _merge_servers(self, data: dict) -> dict:
        """Суммирует battle_servers, seeding_servers и training_servers без перезаписи ключей."""
        all_srv = {}
        for srv, mins in data.get("battle_servers", {}).items():
            all_srv[srv] = all_srv.get(srv, 0) + mins
        for srv, mins in data.get("seeding_servers", {}).items():
            all_srv[srv] = all_srv.get(srv, 0) + mins
        for srv, mins in data.get("training_servers", {}).items():
            all_srv[srv] = all_srv.get(srv, 0) + mins
        return all_srv

    # ==========================================
    # КОМАНДЫ АДМИНИСТРАТОРА
    # ==========================================

    @app_commands.command(name="export_stats", description="[АДМИН] Выгрузить HTML-отчет активности всех бойцов (За всё время)")
    @is_bot_admin()
    async def export_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        users_cursor = await self.users.find().to_list(length=None)
        activity_cursor = await self.activity.find().to_list(length=None)

        if not users_cursor:
            return await interaction.followup.send("База пользователей пуста.")

        act_dict = {doc["_id"]: doc for doc in activity_cursor}
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        rows = []
        for u in users_cursor:
            steam_id = u.get("steam_id", "Unknown")
            d_name   = u.get("discord_name", "Unknown")
            s_nick   = u.get("squad_nickname", "Unknown")

            act      = act_dict.get(steam_id, {})
            battle_h = act.get("total_battle_minutes", 0) / 60
            seed_h   = act.get("total_seeding_minutes", 0) / 60
            train_h  = act.get("total_training_minutes", 0) / 60
            total_h  = act.get("total_minutes", 0) / 60

            user_servers = {}
            for srv_name, mins in act.get("battle_servers", {}).items():
                user_servers[srv_name] = user_servers.get(srv_name, 0) + mins
            for srv_name, mins in act.get("seeding_servers", {}).items():
                user_servers[srv_name] = user_servers.get(srv_name, 0) + mins
            for srv_name, mins in act.get("training_servers", {}).items():
                user_servers[srv_name] = user_servers.get(srv_name, 0) + mins

            sorted_servers = sorted(user_servers.items(), key=lambda x: x[1], reverse=True)
            rows.append((d_name, s_nick, steam_id, battle_h, seed_h, train_h, total_h, sorted_servers))

        rows.sort(key=lambda r: r[6], reverse=True)

        tbody_html = ""
        for idx, (d_name, s_nick, steam_id, battle_h, seed_h, train_h, total_h, sorted_servers) in enumerate(rows, 1):
            total_class = "zero" if total_h == 0 else "good"

            if sorted_servers:
                srv_items = "".join(
                    f"<li>{name}: <b>{mins/60:.1f} ч.</b></li>"
                    for name, mins in sorted_servers
                )
                server_html = (
                    f"<details>"
                    f"<summary class='srv-summary'>Серверов: {len(sorted_servers)}</summary>"
                    f"<ul class='server-list'>{srv_items}</ul>"
                    f"</details>"
                )
            else:
                server_html = "<span class='no-data'>Нет активности</span>"

            tbody_html += (
                f"<tr>"
                f"<td class='num'>{idx}</td>"
                f"<td>{d_name}</td>"
                f"<td class='nick'>{s_nick}</td>"
                f"<td><code>{steam_id}</code></td>"
                f"<td data-val='{battle_h:.2f}'>{battle_h:.1f}</td>"
                f"<td data-val='{seed_h:.2f}'>{seed_h:.1f}</td>"
                f"<td class='training' data-val='{train_h:.2f}'>{train_h:.1f}</td>"
                f"<td class='{total_class}' data-val='{total_h:.2f}'>{total_h:.1f}</td>"
                f"<td>{server_html}</td>"
                f"</tr>\n"
            )

        html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Отчет по активности клана</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      background-color: #1e2124;
      color: #dcddde;
      margin: 0;
      padding: 24px 32px;
    }}
    h1 {{ color: #7289da; text-align: center; margin-bottom: 4px; font-size: 22px; }}
    .date {{ text-align: center; color: #72767d; margin-bottom: 18px; font-size: 13px; }}

    /* ── Панель управления ── */
    .controls {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }}
    #searchInput {{
      flex: 1;
      min-width: 220px;
      padding: 8px 12px;
      border-radius: 6px;
      border: 1px solid #40444b;
      background: #2f3136;
      color: #dcddde;
      font-size: 14px;
      outline: none;
      transition: border-color .2s;
    }}
    #searchInput:focus {{ border-color: #7289da; }}
    #searchInput::placeholder {{ color: #72767d; }}
    .count-badge {{ color: #72767d; font-size: 13px; white-space: nowrap; }}

    /* ── Таблица ── */
    .table-wrap {{
      overflow-x: auto;
      border-radius: 8px;
      box-shadow: 0 4px 12px rgba(0,0,0,.4);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background-color: #23272a;
      min-width: 860px;
    }}
    th, td {{
      padding: 10px 13px;
      text-align: left;
      border-bottom: 1px solid #2c2f33;
      white-space: nowrap;
    }}
    td:last-child {{ white-space: normal; min-width: 180px; }}
    th {{
      background-color: #2f3136;
      color: #b9bbbe;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .04em;
      cursor: pointer;
      user-select: none;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    th.no-sort {{ cursor: default; }}
    th:not(.no-sort):hover {{ background-color: #36393f; color: #ffffff; }}
    th.sort-asc::after  {{ content: " ▲"; color: #7289da; }}
    th.sort-desc::after {{ content: " ▼"; color: #7289da; }}
    tr:hover {{ background-color: #2a2d32; }}
    tr.hidden {{ display: none; }}

    /* ── Ячейки ── */
    .num      {{ color: #72767d; font-size: 12px; text-align: center; width: 36px; }}
    .zero     {{ color: #ed4245; font-weight: 700; }}
    .good     {{ color: #57f287; font-weight: 700; }}
    .nick     {{ color: #fee75c; font-weight: 700; }}
    .training {{ color: #eb459e; font-weight: 700; }}
    .no-data  {{ color: #4f545c; font-style: italic; font-size: 13px; }}

    /* ── Серверы (details) ── */
    details {{ cursor: pointer; }}
    .srv-summary {{
      list-style: none;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 8px;
      border-radius: 4px;
      background: #36393f;
      font-size: 12px;
      color: #b9bbbe;
      transition: background .15s;
    }}
    .srv-summary::-webkit-details-marker {{ display: none; }}
    .srv-summary::before {{ content: "▶"; font-size: 9px; color: #7289da; transition: transform .15s; }}
    details[open] .srv-summary::before {{ transform: rotate(90deg); }}
    details[open] .srv-summary {{ background: #40444b; color: #ffffff; }}
    .server-list {{
      margin: 6px 0 2px 4px;
      padding: 0;
      list-style: none;
      font-size: 13px;
      color: #b9bbbe;
    }}
    .server-list li {{ padding: 2px 0; border-bottom: 1px solid #2c2f3344; }}
    .server-list li:last-child {{ border-bottom: none; }}
  </style>
</head>
<body>
  <h1>📊 Детальный отчет по активности клана (За всё время)</h1>
  <div class="date">Сгенерировано: {current_time}</div>

  <div class="controls">
    <input id="searchInput" type="text" placeholder="🔍  Поиск по нику, Steam ID…" oninput="filterTable()">
    <span class="count-badge" id="countBadge"></span>
  </div>

  <div class="table-wrap">
    <table id="mainTable">
      <thead>
        <tr>
          <th class="no-sort">№</th>
          <th>Discord Ник</th>
          <th>Squad Ник</th>
          <th>Steam ID</th>
          <th>Бой (ч)</th>
          <th>Сидинг (ч)</th>
          <th>Трен. (ч)</th>
          <th>Всего (ч)</th>
          <th class="no-sort">Серверы</th>
        </tr>
      </thead>
      <tbody id="tableBody">
{tbody_html}
      </tbody>
    </table>
  </div>

<script>
  function updateCount() {{
    const total   = document.querySelectorAll('#tableBody tr').length;
    const visible = document.querySelectorAll('#tableBody tr:not(.hidden)').length;
    const badge   = document.getElementById('countBadge');
    badge.textContent = visible === total
      ? `Всего бойцов: ${{total}}`
      : `Показано: ${{visible}} / ${{total}}`;
  }}

  function filterTable() {{
    const q = document.getElementById('searchInput').value.toLowerCase();
    document.querySelectorAll('#tableBody tr').forEach(row => {{
      row.classList.toggle('hidden', q !== '' && !row.textContent.toLowerCase().includes(q));
    }});
    renumber();
    updateCount();
  }}

  function renumber() {{
    let n = 1;
    document.querySelectorAll('#tableBody tr:not(.hidden)').forEach(row => {{
      row.cells[0].textContent = n++;
    }});
  }}

  let lastCol = -1, lastDir = 1;
  document.querySelectorAll('#mainTable thead th').forEach((th, colIdx) => {{
    if (th.classList.contains('no-sort')) return;
    th.addEventListener('click', () => {{
      const dir = (colIdx === lastCol) ? -lastDir : -1;   // первый клик — по убыванию
      lastCol = colIdx; lastDir = dir;
      document.querySelectorAll('#mainTable thead th').forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
      th.classList.add(dir === -1 ? 'sort-desc' : 'sort-asc');

      const tbody = document.getElementById('tableBody');
      Array.from(tbody.querySelectorAll('tr'))
        .sort((a, b) => {{
          const ca = a.cells[colIdx], cb = b.cells[colIdx];
          const va = parseFloat(ca.dataset.val ?? ca.textContent);
          const vb = parseFloat(cb.dataset.val ?? cb.textContent);
          if (!isNaN(va) && !isNaN(vb)) return dir * (vb - va);
          return dir * ca.textContent.trim().localeCompare(cb.textContent.trim(), 'ru');
        }})
        .forEach(r => tbody.appendChild(r));
      renumber();
      updateCount();
    }});
  }});

  updateCount();
</script>
</body>
</html>"""

        file = discord.File(io.BytesIO(html.encode("utf-8")), filename="clan_activity_report.html")
        await interaction.followup.send("**HTML-отчет успешно сгенерирован!**", file=file)

    @app_commands.command(name="link_user", description="[АДМИН] Привязать Discord пользователя к Steam ID")
    @is_bot_admin()
    async def link_user(self, interaction: discord.Interaction, member: discord.Member, steam_id: str, squad_nickname: str):
        await interaction.response.defer(ephemeral=True)

        if await self.users.find_one({"discord_id": member.id}):
            return await interaction.followup.send(f"❌ Пользователь {member.mention} уже привязан к базе. Используйте `/edit_link` для изменения.")

        if await self.users.find_one({"steam_id": steam_id}):
            return await interaction.followup.send(f"❌ Steam ID `{steam_id}` уже занят другим пользователем.")

        await self.users.insert_one({
            "discord_id": member.id,
            "steam_id": steam_id,
            "discord_name": member.name,
            "squad_nickname": squad_nickname
        })
        await interaction.followup.send(f"✅ {member.mention} успешно привязан к Steam ID `{steam_id}` под ником **{squad_nickname}**.")

    @app_commands.command(name="edit_link", description="[АДМИН] Изменить привязку Steam ID или ника")
    @is_bot_admin()
    async def edit_link(self, interaction: discord.Interaction, member: discord.Member, new_steam_id: str, new_squad_nickname: str):
        await interaction.response.defer(ephemeral=True)

        if not await self.users.find_one({"discord_id": member.id}):
            return await interaction.followup.send(f"❌ Пользователь {member.mention} не найден в базе. Используйте `/link_user`.")

        conflict = await self.users.find_one({"steam_id": new_steam_id, "discord_id": {"$ne": member.id}})
        if conflict:
            return await interaction.followup.send(f"❌ Steam ID `{new_steam_id}` уже занят другим бойцом.")

        await self.users.update_one(
            {"discord_id": member.id},
            {"$set": {"steam_id": new_steam_id, "squad_nickname": new_squad_nickname, "discord_name": member.name}}
        )
        await interaction.followup.send(f"✏️ Данные {member.mention} обновлены. Новый Steam ID: `{new_steam_id}`, ник: **{new_squad_nickname}**.")

    @app_commands.command(name="unlink_user", description="[АДМИН] Удалить привязку пользователя из базы")
    @is_bot_admin()
    async def unlink_user(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        result = await self.users.delete_one({"discord_id": member.id})
        if result.deleted_count > 0:
            await interaction.followup.send(f"🗑️ Привязка пользователя {member.mention} успешно удалена.")
        else:
            await interaction.followup.send(f"⚠️ Пользователь {member.mention} и так не был привязан к базе.")

    @app_commands.command(name="check_user", description="[АДМИН] Посмотреть статистику активности конкретного бойца")
    @app_commands.choices(period=PERIOD_CHOICES)
    @is_bot_admin()
    async def check_user(self, interaction: discord.Interaction, member: discord.Member, period: app_commands.Choice[str] = None):
        await interaction.response.defer()
        period_val = period.value if period else "all"
        period_name = period.name if period else "За всё время"

        user_link = await self.users.find_one({"discord_id": member.id})
        if not user_link:
            return await interaction.followup.send("Этот пользователь не привязан к Steam ID.")

        steam_id = user_link["steam_id"]
        squad_nick = user_link.get("squad_nickname", member.display_name)

        data = await self.fetch_user_stats(steam_id, period_val)
        if not data:
            return await interaction.followup.send(f"Данных об активности за **{period_name}** нет.")

        battle_h = data.get("total_battle_minutes", 0) / 60
        seed_h = data.get("total_seeding_minutes", 0) / 60
        training_h = data.get("total_training_minutes", 0) / 60
        total_h = data.get("total_minutes", 0) / 60

        embed = discord.Embed(
            title=f"Статистика: {squad_nick}",
            description=f"Steam ID: {steam_id}\nDiscord: {member.mention}\nПериод: **{period_name}**",
            color=0xe74c3c
        )
        embed.add_field(name="⏱️ Всего", value=f"**{total_h:.1f} ч.**", inline=False)
        embed.add_field(name="⚔️ Бой", value=f"{battle_h:.1f} ч.", inline=True)
        embed.add_field(name="🌱 Сидинг", value=f"{seed_h:.1f} ч.", inline=True)
        embed.add_field(name="🎯 Тренировки", value=f"{training_h:.1f} ч.", inline=True)

        top_srv = sorted(self._merge_servers(data).items(), key=lambda x: x[1], reverse=True)[:5]
        srv_str = "\n".join([f"• {n}: {m/60:.1f}ч" for n, m in top_srv]) or "Нет данных"
        embed.add_field(name="Топ серверов", value=srv_str, inline=False)
        await interaction.followup.send(embed=embed)

    # ==========================================
    # ОБЩИЕ КОМАНДЫ (Для клана)
    # ==========================================

    @app_commands.command(name="my_stats", description="Посмотреть свою статистику активности")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def my_stats(self, interaction: discord.Interaction, period: app_commands.Choice[str] = None):
        await interaction.response.defer()
        period_val = period.value if period else "all"
        period_name = period.name if period else "За всё время"

        user_link = await self.users.find_one({"discord_id": interaction.user.id})
        if not user_link:
            return await interaction.followup.send("Вы не привязаны к базе данных клана. Обратитесь к офицерам.")

        steam_id = user_link["steam_id"]
        squad_nick = user_link.get("squad_nickname", interaction.user.display_name)

        data = await self.fetch_user_stats(steam_id, period_val)
        if not data:
            return await interaction.followup.send(f"Данных об активности за период **{period_name}** нет.")

        battle_h = data.get("total_battle_minutes", 0) / 60
        seed_h = data.get("total_seeding_minutes", 0) / 60
        training_h = data.get("total_training_minutes", 0) / 60
        total_h = data.get("total_minutes", 0) / 60

        embed = discord.Embed(title=f"Твоя статистика: {squad_nick}", description=f"Период: **{period_name}**", color=0x2ecc71)
        embed.add_field(name="⏱️ Всего наиграно", value=f"**{total_h:.1f} ч.**", inline=False)
        embed.add_field(name="⚔️ Время в бою", value=f"**{battle_h:.1f} ч.**", inline=True)
        embed.add_field(name="🌱 Время сидинга", value=f"**{seed_h:.1f} ч.**", inline=True)
        if training_h > 0:
            embed.add_field(name="🎯 Тренировки", value=f"**{training_h:.1f} ч.**", inline=True)

        top_srv = sorted(self._merge_servers(data).items(), key=lambda x: x[1], reverse=True)[:3]
        if top_srv:
            srv_str = "\n".join([f"• {n}: {m/60:.1f}ч" for n, m in top_srv])
            embed.add_field(name="Любимые серверы", value=srv_str, inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="top_players", description="Показать топ 10 игроков по активности")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def top_players(self, interaction: discord.Interaction, period: app_commands.Choice[str] = None):
        await interaction.response.defer()
        period_val = period.value if period else "all"
        period_name = period.name if period else "За всё время"

        players = []
        if period_val == "all":
            cursor = self.activity.find().sort("total_minutes", -1).limit(10)
            players = await cursor.to_list(length=10)
        else:
            target_date = (datetime.datetime.utcnow() - datetime.timedelta(days=int(period_val))).strftime('%Y-%m-%d')
            pipeline = [
                {"$match": {"date": {"$gte": target_date}}},
                {"$group": {"_id": "$steam_id", "total_minutes": {"$sum": "$total_minutes"}}},
                {"$sort": {"total_minutes": -1}},
                {"$limit": 10}
            ]
            players = await self.daily_activity.aggregate(pipeline).to_list(length=10)

        if not players:
            return await interaction.followup.send(f"Данных за период **{period_name}** пока нет.")

        users_cursor = await self.users.find().to_list(length=None)
        steam_to_nick = {doc["steam_id"]: doc.get("squad_nickname", "Unknown") for doc in users_cursor}

        embed = discord.Embed(title=f"🏆 Топ 10 самых активных ({period_name})", color=0x3498db)
        description = ""
        for i, p in enumerate(players, 1):
            steam_id = p["_id"]
            nick = steam_to_nick.get(steam_id, "Неизвестный боец")
            total_h = p.get("total_minutes", 0) / 60
            description += f"**{i}.** {nick} — **{total_h:.1f} ч.**\n"
        embed.description = description
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="top_servers", description="Показать топ серверов, где играет клан")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def top_servers(self, interaction: discord.Interaction, period: app_commands.Choice[str] = None):
        await interaction.response.defer()
        period_val = period.value if period else "all"
        period_name = period.name if period else "За всё время"

        server_totals = {}

        if period_val == "all":
            cursor = self.activity.find()
        else:
            target_date = (datetime.datetime.utcnow() - datetime.timedelta(days=int(period_val))).strftime('%Y-%m-%d')
            cursor = self.daily_activity.find({"date": {"$gte": target_date}})

        async for doc in cursor:
            for srv_name, minutes in doc.get("battle_servers", {}).items():
                server_totals[srv_name] = server_totals.get(srv_name, 0) + minutes
            for srv_name, minutes in doc.get("seeding_servers", {}).items():
                server_totals[srv_name] = server_totals.get(srv_name, 0) + minutes
            for srv_name, minutes in doc.get("training_servers", {}).items():
                server_totals[srv_name] = server_totals.get(srv_name, 0) + minutes

        if not server_totals:
            return await interaction.followup.send(f"Данных о серверах за **{period_name}** пока нет.")

        top_srv = sorted(server_totals.items(), key=lambda x: x[1], reverse=True)[:10]

        embed = discord.Embed(title=f"🌍 Топ 10 серверов клана ({period_name})", color=0x9b59b6)
        description = ""
        for i, (srv_name, minutes) in enumerate(top_srv, 1):
            description += f"**{i}.** {srv_name} — **{minutes/60:.1f} ч.**\n"
        embed.description = description
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="clan_stats", description="Показать общую статистику всего клана")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def clan_stats(self, interaction: discord.Interaction, period: app_commands.Choice[str] = None):
        await interaction.response.defer()
        period_val = period.value if period else "all"
        period_name = period.name if period else "За всё время"

        pipeline = []
        if period_val != "all":
            target_date = (datetime.datetime.utcnow() - datetime.timedelta(days=int(period_val))).strftime('%Y-%m-%d')
            pipeline.append({"$match": {"date": {"$gte": target_date}}})

        pipeline.append({
            "$group": {
                "_id": None,
                "overall_battle": {"$sum": "$total_battle_minutes"},
                "overall_seeding": {"$sum": "$total_seeding_minutes"},
                "overall_training": {"$sum": "$total_training_minutes"},
                "overall_total": {"$sum": "$total_minutes"}
            }
        })

        collection = self.activity if period_val == "all" else self.daily_activity
        result = await collection.aggregate(pipeline).to_list(length=1)

        if not result:
            return await interaction.followup.send(f"Нет данных для статистики за **{period_name}**.")

        stats = result[0]
        total_h = stats.get("overall_total", 0) / 60
        battle_h = stats.get("overall_battle", 0) / 60
        seed_h = stats.get("overall_seeding", 0) / 60
        training_h = stats.get("overall_training", 0) / 60

        player_count = len(await collection.distinct(
            "steam_id" if period_val != "all" else "_id",
            pipeline[0]["$match"] if period_val != "all" else {}
        ))

        embed = discord.Embed(title=f"📊 Глобальная статистика клана ({period_name})", color=0xf1c40f)
        embed.add_field(name="Всего наиграно", value=f"**{total_h:.1f} ч.**", inline=False)
        embed.add_field(name="В боях", value=f"{battle_h:.1f} ч.", inline=True)
        embed.add_field(name="На сидинге", value=f"{seed_h:.1f} ч.", inline=True)
        embed.add_field(name="На тренировках", value=f"{training_h:.1f} ч.", inline=True)
        embed.set_footer(text=f"Активных бойцов за этот период: {player_count}")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ActivityTracker(bot))