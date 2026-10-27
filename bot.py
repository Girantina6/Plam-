import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN', '')
DB_PATH = os.getenv('DATABASE_PATH', 'data/nexus.db')
GUILD_ID = os.getenv('GUILD_ID', '').strip()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('Nexus')

DEFAULT_QUESTIONS = ['Как к вам обращаться?', 'Расскажите о своём опыте', 'Почему вы хотите присоединиться?']

class Nexus(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.default())
        self.db = None

    async def setup_hook(self):
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript('''
        CREATE TABLE IF NOT EXISTS forms (
          guild_id INTEGER NOT NULL, name TEXT NOT NULL, channel_id INTEGER NOT NULL,
          reviewer_role_id INTEGER, approved_role_id INTEGER, questions TEXT NOT NULL,
          PRIMARY KEY(guild_id,name));
        CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          form_name TEXT NOT NULL, user_id INTEGER NOT NULL, answers TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', reviewer_id INTEGER,
          reason TEXT, message_id INTEGER, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_app_user ON applications(guild_id,form_name,user_id,status);
        ''')
        await self.db.commit()
        self.add_view(ApplyView(self))
        self.add_view(ReviewView(self))
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()

bot = Nexus()

def can_review(member, role_id):
    return member.guild_permissions.manage_guild or (role_id is not None and any(r.id == role_id for r in member.roles))

async def get_form(guild_id, name):
    async with bot.db.execute('SELECT * FROM forms WHERE guild_id=? AND name=?', (guild_id, name)) as cursor:
        return await cursor.fetchone()

class ApplicationModal(discord.ui.Modal):
    def __init__(self, guild_id, form_name, questions):
        super().__init__(title=f'Анкета: {form_name}'[:45], timeout=600)
        self.guild_id, self.form_name = guild_id, form_name
        self.inputs = []
        for q in questions[:5]:
            field = discord.ui.TextInput(label=q[:45], style=discord.TextStyle.paragraph, max_length=1000, required=True)
            self.inputs.append(field)
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        form = await get_form(self.guild_id, self.form_name)
        if not form:
            return await interaction.response.send_message('Эта анкета больше недоступна.', ephemeral=True)
        async with bot.db.execute('SELECT id FROM applications WHERE guild_id=? AND form_name=? AND user_id=? AND status=?',
                                  (self.guild_id, self.form_name, interaction.user.id, 'pending')) as cursor:
            if await cursor.fetchone():
                return await interaction.response.send_message('У вас уже есть заявка на рассмотрении.', ephemeral=True)
        channel = interaction.guild.get_channel(form['channel_id'])
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message('Канал рассмотрения недоступен. Обратитесь к администрации.', ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = [(field.label, field.value) for field in self.inputs]
        async with bot.db.execute('INSERT INTO applications(guild_id,form_name,user_id,answers,created_at) VALUES(?,?,?,?,?)',
                                  (self.guild_id,self.form_name,interaction.user.id,json.dumps(answers,ensure_ascii=False),datetime.now(timezone.utc).isoformat())) as cursor:
            app_id = cursor.lastrowid
        await bot.db.commit()
        embed = make_embed(app_id, self.form_name, interaction.user.id, answers, 'pending')
        try:
            msg = await channel.send(embed=embed, view=ReviewView(bot))
        except discord.HTTPException:
            await bot.db.execute('DELETE FROM applications WHERE id=?', (app_id,))
            await bot.db.commit()
            return await interaction.followup.send('Не удалось отправить заявку. Попробуйте позже.', ephemeral=True)
        await bot.db.execute('UPDATE applications SET message_id=? WHERE id=?', (msg.id, app_id))
        await bot.db.commit()
        await interaction.followup.send(f'Заявка №{app_id} успешно отправлена!', ephemeral=True)

def make_embed(app_id, form_name, user_id, answers, status, reviewer_id=None, reason=None):
    colors = {'pending':discord.Color.gold(),'approved':discord.Color.green(),'rejected':discord.Color.red()}
    names = {'pending':'На рассмотрении','approved':'Одобрена','rejected':'Отклонена'}
    embed = discord.Embed(title=f'Заявка #{app_id} · {form_name}', color=colors[status])
    embed.add_field(name='Участник',value=f'<@{user_id}> (`{user_id}`)',inline=False)
    for q, a in answers:
        embed.add_field(name=q[:256],value=(a[:1021]+'...' if len(a)>1024 else a) or '—',inline=False)
    embed.add_field(name='Статус',value=names[status],inline=False)
    if reviewer_id:
        embed.add_field(name='Рассмотрел',value=f'<@{reviewer_id}>')
    if reason:
        embed.add_field(name='Причина',value=reason[:1024],inline=False)
    return embed

class ApplyView(discord.ui.View):
    def __init__(self, client):
        super().__init__(timeout=None)
        self.client = client

    @discord.ui.button(label='Подать заявку',style=discord.ButtonStyle.primary,custom_id='nexus:apply')
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.message or not interaction.message.embeds:
            return await interaction.response.send_message('Форма недоступна.',ephemeral=True)
        # Panel identity is stored in the embed footer, not supplied by the applicant.
        footer = interaction.message.embeds[0].footer.text or ''
        if not footer.startswith('nexus-form:'):
            return await interaction.response.send_message('Форма недоступна.',ephemeral=True)
        name = footer.removeprefix('nexus-form:')
        form = await get_form(interaction.guild.id,name)
        if not form:
            return await interaction.response.send_message('Форма удалена.',ephemeral=True)
        await interaction.response.send_modal(ApplicationModal(interaction.guild.id,name,json.loads(form['questions'])))

class RejectModal(discord.ui.Modal,title='Причина отказа'):
    reason = discord.ui.TextInput(label='Причина',style=discord.TextStyle.paragraph,max_length=500)
    def __init__(self, app_id):
        super().__init__()
        self.app_id = app_id
    async def on_submit(self, interaction):
        await decide(interaction,self.app_id,'rejected',str(self.reason))

class ReviewView(discord.ui.View):
    def __init__(self, client):
        super().__init__(timeout=None)
        self.client = client

    @discord.ui.button(label='Принять',style=discord.ButtonStyle.success,custom_id='nexus:approve')
    async def approve(self, interaction, button):
        await self.handle(interaction,'approved')

    @discord.ui.button(label='Отклонить',style=discord.ButtonStyle.danger,custom_id='nexus:reject')
    async def reject(self, interaction, button):
        await self.handle(interaction,'rejected')

    async def handle(self, interaction, status):
        if not interaction.guild or not interaction.message or not interaction.message.embeds:
            return await interaction.response.send_message('Заявка недоступна.',ephemeral=True)
        title = interaction.message.embeds[0].title or ''
        try:
            app_id = int(title.split(' · ')[0].removeprefix('Заявка #'))
        except ValueError:
            return await interaction.response.send_message('Неверный формат заявки.',ephemeral=True)
        async with bot.db.execute('SELECT * FROM applications WHERE id=? AND guild_id=? AND message_id=?',
                                  (app_id,interaction.guild.id,interaction.message.id)) as cur:
            app = await cur.fetchone()
        if not app:
            return await interaction.response.send_message('Заявка не найдена.',ephemeral=True)
        form = await get_form(interaction.guild.id,app['form_name'])
        if not can_review(interaction.user,form['reviewer_role_id'] if form else None):
            return await interaction.response.send_message('Недостаточно прав.',ephemeral=True)
        if status == 'rejected':
            return await interaction.response.send_modal(RejectModal(app_id))
        await decide(interaction,app_id,status,None)

async def decide(interaction,app_id,status,reason):
    if not interaction.guild or not interaction.message:
        return await interaction.response.send_message('Недоступно.',ephemeral=True)
    async with bot.db.execute('SELECT * FROM applications WHERE id=? AND guild_id=? AND message_id=?',
                              (app_id,interaction.guild.id,interaction.message.id)) as cur:
        app = await cur.fetchone()
    if not app:
        return await interaction.response.send_message('Заявка не найдена.',ephemeral=True)
    form = await get_form(interaction.guild.id,app['form_name'])
    if not can_review(interaction.user,form['reviewer_role_id'] if form else None):
        return await interaction.response.send_message('Недостаточно прав.',ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cur = await bot.db.execute('UPDATE applications SET status=?,reviewer_id=?,reason=? WHERE id=? AND status=?',
                               (status,interaction.user.id,reason,app_id,'pending'))
    await bot.db.commit()
    if cur.rowcount != 1:
        return await interaction.followup.send('Заявка уже рассмотрена.',ephemeral=True)
    embed = make_embed(app_id,app['form_name'],app['user_id'],json.loads(app['answers']),status,interaction.user.id,reason)
    try:
        await interaction.message.edit(embed=embed,view=None)
    except discord.HTTPException:
        log.exception('Failed to update application message %s',app_id)
    role_note = ''
    if status == 'approved' and form and form['approved_role_id']:
        role = interaction.guild.get_role(form['approved_role_id'])
        member = interaction.guild.get_member(app['user_id'])
        if role and member:
            try:
                await member.add_roles(role,reason=f'Application #{app_id} approved')
            except discord.HTTPException:
                role_note = ' Роль не выдана: проверьте права и иерархию ролей.'
    try:
        user = await bot.fetch_user(app['user_id'])
        await user.send(f'Ваша заявка #{app_id} ({app["form_name"]}): {"одобрена" if status == "approved" else "отклонена"}.' + (f' Причина: {reason}' if reason else ''))
    except discord.HTTPException:
        pass
    await interaction.followup.send('Решение сохранено.'+role_note,ephemeral=True)

@bot.tree.command(name='form_create',description='Создать форму заявок (до пяти вопросов)')
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(name='Название формы',review_channel='Канал для рассмотрения',reviewer_role='Роль проверяющих',approved_role='Роль после одобрения')
async def form_create(interaction:discord.Interaction,name:str,review_channel:discord.TextChannel,reviewer_role:discord.Role|None=None,approved_role:discord.Role|None=None):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message('Требуется право управления сервером.',ephemeral=True)
    if len(name)>40 or not name.strip():
        return await interaction.response.send_message('Название должно содержать от 1 до 40 символов.',ephemeral=True)
    try:
        await bot.db.execute('INSERT INTO forms VALUES(?,?,?,?,?,?)',
                             (interaction.guild.id,name,review_channel.id,reviewer_role.id if reviewer_role else None,approved_role.id if approved_role else None,json.dumps(DEFAULT_QUESTIONS,ensure_ascii=False)))
        await bot.db.commit()
    except aiosqlite.IntegrityError:
        return await interaction.response.send_message('Форма с таким названием уже существует.',ephemeral=True)
    await interaction.response.send_message(f'Форма **{name}** создана. Настройте вопросы через `/form_questions`, затем опубликуйте `/form_publish`.',ephemeral=True)

@bot.tree.command(name='form_questions',description='Изменить вопросы анкеты (через символ |)')
@app_commands.default_permissions(manage_guild=True)
async def form_questions(interaction:discord.Interaction,name:str,questions:str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message('Недостаточно прав.',ephemeral=True)
    parts = [s.strip() for s in questions.split('|')]
    if not 1<=len(parts)<=5 or any(not q or len(q)>45 for q in parts):
        return await interaction.response.send_message('Нужно 1–5 вопросов, каждый до 45 символов, разделённых |.',ephemeral=True)
    cur = await bot.db.execute('UPDATE forms SET questions=? WHERE guild_id=? AND name=?',
                               (json.dumps(parts,ensure_ascii=False),interaction.guild.id,name))
    await bot.db.commit()
    await interaction.response.send_message('Вопросы обновлены.' if cur.rowcount else 'Форма не найдена.',ephemeral=True)

@bot.tree.command(name='form_publish',description='Опубликовать панель подачи заявок')
@app_commands.default_permissions(manage_guild=True)
async def form_publish(interaction:discord.Interaction,name:str,channel:discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message('Недостаточно прав.',ephemeral=True)
    form = await get_form(interaction.guild.id,name)
    if not form:
        return await interaction.response.send_message('Форма не найдена.',ephemeral=True)
    embed = discord.Embed(title=f'Анкета: {name}',description='Нажмите кнопку ниже, чтобы заполнить заявку.',color=discord.Color.blurple())
    embed.set_footer(text=f'nexus-form:{name}')
    await channel.send(embed=embed,view=ApplyView(bot))
    await interaction.response.send_message('Панель опубликована.',ephemeral=True)

@bot.tree.command(name='form_list',description='Список форм сервера')
async def form_list(interaction:discord.Interaction):
    async with bot.db.execute('SELECT name FROM forms WHERE guild_id=? ORDER BY name',(interaction.guild.id,)) as cur:
        names = [row['name'] for row in await cur.fetchall()]
    await interaction.response.send_message('Формы: '+(', '.join(names) if names else 'пока нет'),ephemeral=True)

@bot.event
async def on_ready():
    log.info('Online as %s',bot.user)

if __name__ == '__main__':
    if not TOKEN:
        raise SystemExit('Set DISCORD_TOKEN in environment')
    bot.run(TOKEN)
