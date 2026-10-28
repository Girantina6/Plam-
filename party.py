"""Party recruitment extension for Plam v4."""
import asyncio
import os
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands

COOLDOWN = timedelta(hours=24)

def env_id(name):
    raw = os.getenv(name, '').strip()
    if not raw.isdecimal() or int(raw) <= 0:
        raise RuntimeError(f'Укажите корректный {name} в переменных окружения')
    return int(raw)

ADMIN_ROLE_ID = env_id('ADMIN_ROLE_ID')
MASTER_ROLE_ID = env_id('MASTER_ROLE_ID')
PARTY_CHANNEL_ID = env_id('PARTY_CHANNEL_ID')


def register(bot):
    lock = asyncio.Lock()

    async def fetch(sql, args=()):
        async with bot.db.execute(sql, args) as cur:
            return await cur.fetchone()

    async def rows(sql, args=()):
        async with bot.db.execute(sql, args) as cur:
            return await cur.fetchall()

    def admin(member):
        return isinstance(member, discord.Member) and (member.id == member.guild.owner_id or any(role.id == ADMIN_ROLE_ID for role in member.roles))

    async def init():
        await bot.db.executescript('''
        CREATE TABLE IF NOT EXISTS party_posts(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, master_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, required INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS party_members(post_id INTEGER NOT NULL, user_id INTEGER NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(post_id,user_id));
        CREATE INDEX IF NOT EXISTS party_cooldown ON party_posts(guild_id,master_id,created_at);
        ''')
        await bot.db.commit()
        bot.add_view(PartyView())

    async def update_post(post_id):
        post = await fetch('SELECT * FROM party_posts WHERE id=?', (post_id,))
        if not post or post['status'] != 'active':
            return
        members = await rows('SELECT user_id,confirmed FROM party_members WHERE post_id=? ORDER BY rowid', (post_id,))
        confirmed = sum(m['confirmed'] for m in members)
        channel = bot.get_channel(post['channel_id'])
        if channel is None:
            try:
                channel = await bot.fetch_channel(post['channel_id'])
            except (discord.HTTPException, discord.Forbidden):
                return
        if confirmed >= post['required']:
            await bot.db.execute("UPDATE party_posts SET status='completed' WHERE id=? AND status='active'", (post_id,))
            await bot.db.commit()
            if post['message_id']:
                try:
                    await (await channel.fetch_message(post['message_id'])).delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            return
        embed = discord.Embed(title=f'🛡️ Поиск партии №{post_id}: {post["title"]}', description=post['description'], color=discord.Color.blurple())
        embed.add_field(name='Мастер', value=f'<@{post["master_id"]}>')
        embed.add_field(name='Подтверждено', value=f'{confirmed}/{post["required"]}')
        embed.add_field(name='Записались', value='\n'.join(f'{"✅" if m["confirmed"] else "▫️"} <@{m["user_id"]}>' for m in members)[:1024] or 'Пока никого')
        embed.set_footer(text='Мастер подтверждает присутствие участников через команду /party_confirm')
        if post['message_id']:
            try:
                await (await channel.fetch_message(post['message_id'])).edit(embed=embed, view=PartyView(), allowed_mentions=discord.AllowedMentions.none())
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    class PartyView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label='Записаться', style=discord.ButtonStyle.success, custom_id='plam:party:join:v1')
        async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.guild or not interaction.message:
                return await interaction.response.send_message('Недоступно.', ephemeral=True)
            async with lock:
                post = await fetch("SELECT * FROM party_posts WHERE guild_id=? AND message_id=? AND status='active'", (interaction.guild.id, interaction.message.id))
                if not post:
                    return await interaction.response.send_message('Набор завершён.', ephemeral=True)
                existing = await fetch('SELECT 1 FROM party_members WHERE post_id=? AND user_id=?', (post['id'], interaction.user.id))
                if existing:
                    return await interaction.response.send_message('Вы уже записаны.', ephemeral=True)
                enrolled = await fetch('SELECT COUNT(*) AS n FROM party_members WHERE post_id=?', (post['id'],))
                if enrolled['n'] >= post['required']:
                    return await interaction.response.send_message('Все места заняты.', ephemeral=True)
                await bot.db.execute('INSERT INTO party_members(post_id,user_id) VALUES(?,?)', (post['id'], interaction.user.id))
                await bot.db.commit()
                await interaction.response.send_message('Вы записаны. Мастер подтвердит ваше присутствие.', ephemeral=True)
                await update_post(post['id'])

    @bot.tree.command(name='party_create', description='Мастер: опубликовать набор в партию')
    @app_commands.guild_only()
    async def party_create(interaction: discord.Interaction, title: str, description: str, participants: app_commands.Range[int,1,25]):
        if len(title)>100 or len(description)>1500:
            return await interaction.response.send_message('Название до 100, описание до 1500 символов.', ephemeral=True)
        if not (admin(interaction.user) or any(r.id == MASTER_ROLE_ID for r in interaction.user.roles)):
            return await interaction.response.send_message('Нужна роль мастера.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        async with lock:
            active = await fetch("SELECT id FROM party_posts WHERE guild_id=? AND master_id=? AND status='active'", (interaction.guild.id,interaction.user.id))
            if active:
                return await interaction.followup.send('У вас уже есть активный набор.', ephemeral=True)
            last = await fetch('SELECT created_at FROM party_posts WHERE guild_id=? AND master_id=? ORDER BY id DESC LIMIT 1', (interaction.guild.id,interaction.user.id))
            if last:
                next_time = datetime.fromisoformat(last['created_at']) + COOLDOWN
                if datetime.now(timezone.utc)<next_time:
                    return await interaction.followup.send(f'Следующая публикация доступна <t:{int(next_time.timestamp())}:R>.', ephemeral=True)
            channel = bot.get_channel(PARTY_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(PARTY_CHANNEL_ID)
                except (discord.HTTPException,discord.Forbidden):
                    return await interaction.followup.send('Канал недоступен боту.', ephemeral=True)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != interaction.guild.id:
                return await interaction.followup.send('PARTY_CHANNEL_ID должен указывать на текстовый канал этого сервера.', ephemeral=True)
            created = datetime.now(timezone.utc).isoformat()
            cur = await bot.db.execute('INSERT INTO party_posts(guild_id,master_id,title,description,required,channel_id,created_at) VALUES(?,?,?,?,?,?,?)', (interaction.guild.id,interaction.user.id,title,description,participants,channel.id,created))
            post_id = cur.lastrowid
            await bot.db.commit()
            embed = discord.Embed(title=f'🛡️ Поиск партии №{post_id}: {title}', description=description, color=discord.Color.blurple())
            embed.add_field(name='Мастер',value=interaction.user.mention)
            embed.add_field(name='Подтверждено',value=f'0/{participants}')
            embed.set_footer(text='Запишитесь кнопкой. Мастер подтвердит присутствие.')
            try:
                message = await channel.send(embed=embed,view=PartyView(),allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden,discord.HTTPException):
                await bot.db.execute('DELETE FROM party_posts WHERE id=?',(post_id,))
                await bot.db.commit()
                return await interaction.followup.send('Не удалось опубликовать: проверьте права бота.',ephemeral=True)
            await bot.db.execute('UPDATE party_posts SET message_id=? WHERE id=?',(message.id,post_id))
            await bot.db.commit()
        await interaction.followup.send(f'Набор опубликован: {message.jump_url}',ephemeral=True)

    @bot.tree.command(name='party_confirm', description='Мастер: подтвердить присутствие записанного участника')
    @app_commands.guild_only()
    async def party_confirm(interaction: discord.Interaction, post_id: int, member: discord.Member, present: bool=True):
        async with lock:
            post = await fetch('SELECT * FROM party_posts WHERE id=? AND guild_id=? AND status=?',(post_id,interaction.guild.id,'active'))
            if not post:
                return await interaction.response.send_message('Активный набор не найден.',ephemeral=True)
            if interaction.user.id != post['master_id'] and not admin(interaction.user):
                return await interaction.response.send_message('Только мастер набора или администратор.',ephemeral=True)
            entry = await fetch('SELECT 1 FROM party_members WHERE post_id=? AND user_id=?',(post_id,member.id))
            if not entry:
                return await interaction.response.send_message('Участник не записан на этот набор.',ephemeral=True)
            await bot.db.execute('UPDATE party_members SET confirmed=? WHERE post_id=? AND user_id=?',(int(present),post_id,member.id))
            await bot.db.commit()
            await interaction.response.send_message('Присутствие подтверждено.' if present else 'Подтверждение снято.',ephemeral=True)
            await update_post(post_id)

    @bot.tree.command(name='party_cancel',description='Отменить свой набор (администратор может отменить любой)')
    @app_commands.guild_only()
    async def party_cancel(interaction: discord.Interaction, post_id: int):
        async with lock:
            post = await fetch("SELECT * FROM party_posts WHERE id=? AND guild_id=? AND status='active'",(post_id,interaction.guild.id))
            if not post:
                return await interaction.response.send_message('Активный набор не найден.',ephemeral=True)
            if interaction.user.id != post['master_id'] and not admin(interaction.user):
                return await interaction.response.send_message('Недостаточно прав.',ephemeral=True)
            await bot.db.execute("UPDATE party_posts SET status='cancelled' WHERE id=?",(post_id,))
            await bot.db.commit()
            channel = bot.get_channel(post['channel_id'])
            if channel is not None and post['message_id']:
                try:
                    await (await channel.fetch_message(post['message_id'])).delete()
                except (discord.HTTPException,discord.Forbidden):
                    pass
        await interaction.response.send_message('Набор отменён. Ограничение 24 часа сохраняется.',ephemeral=True)

    return init
