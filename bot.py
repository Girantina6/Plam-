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
from party import register

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN', '')
DB_PATH = os.getenv('DATABASE_PATH', 'data/plam.db')
GUILD_ID = os.getenv('GUILD_ID', '').strip()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('Plam')


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_options(raw):
    return [x.strip() for x in raw.split('|') if x.strip()]


class Plam(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = False  # DM messages remain accessible without privileged intent
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = None
        self.user_locks = {}
        self.review_lock = asyncio.Lock()

    async def setup_hook(self):
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript('''
        CREATE TABLE IF NOT EXISTS forms (
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          name TEXT NOT NULL, channel_id INTEGER NOT NULL, admin_role_id INTEGER,
          approved_role_id INTEGER, UNIQUE(guild_id,name));
        CREATE TABLE IF NOT EXISTS questions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, form_id INTEGER NOT NULL,
          position INTEGER NOT NULL, text TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('text','choice')),
          options_json TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS sessions (
          user_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, form_id INTEGER NOT NULL,
          position INTEGER NOT NULL DEFAULT 0, answers_json TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          form_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          answers_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          reviewer_id INTEGER, reason TEXT, message_id INTEGER,
          created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_apps ON applications(guild_id,form_id,user_id,status);
        ''')
        await self.db.commit()
        await self.party_init()
        self.add_view(ApplyView())
        self.add_view(ReviewView())
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


bot = Plam()
bot.party_init = register(bot)


async def one(sql, args=()):
    async with bot.db.execute(sql, args) as cur:
        return await cur.fetchone()


async def all_rows(sql, args=()):
    async with bot.db.execute(sql, args) as cur:
        return await cur.fetchall()


async def get_form(guild_id, name):
    return await one('SELECT * FROM forms WHERE guild_id=? AND name=?', (guild_id, name))


def admin_ok(member, form):
    if not isinstance(member, discord.Member):
        return False
    return (member.id == member.guild.owner_id
            or member.guild_permissions.administrator
            or bool(form['admin_role_id'] and any(r.id == form['admin_role_id'] for r in member.roles)))


async def admin_form(interaction, name):
    if not interaction.guild:
        await interaction.response.send_message('Команда доступна только на сервере.', ephemeral=True)
        return None
    form = await get_form(interaction.guild.id, name)
    if not form or not admin_ok(interaction.user, form):
        await interaction.response.send_message('Форма не найдена или нет прав администратора.', ephemeral=True)
        return None
    return form


def split_text(s, limit=1850):
    # Preserve all characters, including very long answers.
    return [s[i:i+limit] for i in range(0, len(s), limit)] or ['—']


async def send_question(user, session):
    questions = await all_rows('SELECT * FROM questions WHERE form_id=? ORDER BY position,id', (session['form_id'],))
    pos = session['position']
    if pos >= len(questions):
        return
    q = questions[pos]
    header = f'**Вопрос {pos+1}/{len(questions)}**\n{q["text"]}'
    if q['kind'] == 'choice':
        options = json.loads(q['options_json'])
        header += '\n\n' + '\n'.join(f'**{i}.** {v}' for i, v in enumerate(options, 1))
        header += '\n\nОтправьте номер варианта.'
    else:
        header += '\n\nНапишите ответ следующим сообщением.'
    for chunk in split_text(header):
        await user.send(chunk)


async def finish(user, session, answers):
    form = await one('SELECT * FROM forms WHERE id=?', (session['form_id'],))
    if not form:
        await bot.db.execute('DELETE FROM sessions WHERE user_id=?', (user.id,))
        await bot.db.commit()
        await user.send('Форма была удалена.')
        return
    channel = bot.get_channel(form['channel_id'])
    if not isinstance(channel, discord.TextChannel):
        await user.send('Канал заявок недоступен. Ответы сохранены как черновик, обратитесь к администратору.')
        return
    async with bot.db.execute('INSERT INTO applications(guild_id,form_id,user_id,answers_json,created_at) VALUES(?,?,?,?,?)',
                              (form['guild_id'], form['id'], user.id, json.dumps(answers, ensure_ascii=False), now())) as cur:
        app_id = cur.lastrowid
    await bot.db.commit()
    try:
        msg = await channel.send(f'**{app_id}. <@{user.id}> отправил(а) форму «{discord.utils.escape_markdown(form["name"])}»**\nСтатус: на рассмотрении', view=ReviewView())
        # Replies are posted as separate messages in the same channel, so there is no embed limit.
        for n, entry in enumerate(answers, 1):
            text = f'**{n}. {entry["question"]}**\n{entry["answer"]}'
            for chunk in split_text(text):
                await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        await bot.db.execute('DELETE FROM applications WHERE id=?', (app_id,))
        await bot.db.commit()
        await user.send('Не удалось опубликовать анкету. Попробуйте позже.')
        return
    await bot.db.execute('UPDATE applications SET message_id=? WHERE id=?', (msg.id, app_id))
    await bot.db.execute('DELETE FROM sessions WHERE user_id=?', (user.id,))
    await bot.db.commit()
    await user.send(f'Анкета №{app_id} отправлена. Спасибо!')


@bot.event
async def on_message(message):
    if message.author.bot or message.guild is not None:
        return
    user_id = message.author.id
    lock = bot.user_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        session = await one('SELECT * FROM sessions WHERE user_id=?', (user_id,))
        if not session:
            return
        if message.content.strip().lower() == 'отмена':
            await bot.db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
            await bot.db.commit()
            await message.channel.send('Заполнение отменено.')
            return
        questions = await all_rows('SELECT * FROM questions WHERE form_id=? ORDER BY position,id', (session['form_id'],))
        if session['position'] >= len(questions):
            return
        q = questions[session['position']]
        answer = message.content.strip()
        if not answer:
            await message.channel.send('Отправьте текстовый ответ.')
            return
        if q['kind'] == 'choice':
            options = json.loads(q['options_json'])
            if not answer.isdecimal() or not 1 <= int(answer) <= len(options):
                await message.channel.send(f'Укажите номер от 1 до {len(options)}.')
                return
            answer = options[int(answer)-1]
        answers = json.loads(session['answers_json'])
        answers.append({'question': q['text'], 'answer': answer})
        pos = session['position'] + 1
        await bot.db.execute('UPDATE sessions SET position=?,answers_json=? WHERE user_id=?',
                             (pos, json.dumps(answers, ensure_ascii=False), user_id))
        await bot.db.commit()
        if pos == len(questions):
            await finish(message.author, session, answers)
        else:
            updated = await one('SELECT * FROM sessions WHERE user_id=?', (user_id,))
            await send_question(message.author, updated)


class ApplyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Заполнить анкету в ЛС', style=discord.ButtonStyle.primary, custom_id='plam:apply:v2')
    async def apply(self, interaction: discord.Interaction, button):
        if not interaction.guild or not interaction.message or not interaction.message.embeds:
            return await interaction.response.send_message('Панель недоступна.', ephemeral=True)
        footer = interaction.message.embeds[0].footer.text or ''
        if not footer.startswith('plam-form:'):
            return await interaction.response.send_message('Панель недоступна.', ephemeral=True)
        try:
            form_id = int(footer.split(':', 1)[1])
        except ValueError:
            return await interaction.response.send_message('Неверная панель.', ephemeral=True)
        form = await one('SELECT * FROM forms WHERE id=? AND guild_id=?', (form_id, interaction.guild.id))
        if not form:
            return await interaction.response.send_message('Форма не найдена.', ephemeral=True)
        questions = await all_rows('SELECT * FROM questions WHERE form_id=? ORDER BY position,id', (form_id,))
        if not questions:
            return await interaction.response.send_message('В этой анкете ещё нет вопросов.', ephemeral=True)
        lock = bot.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        async with lock:
            if await one('SELECT user_id FROM sessions WHERE user_id=?', (interaction.user.id,)):
                return await interaction.response.send_message('Вы уже заполняете анкету. Напишите «отмена» в ЛС боту, чтобы начать другую.', ephemeral=True)
            if await one('SELECT id FROM applications WHERE form_id=? AND user_id=? AND status=?', (form_id, interaction.user.id, 'pending')):
                return await interaction.response.send_message('Ваша заявка уже рассматривается.', ephemeral=True)
            try:
                dm = await interaction.user.create_dm()
                await dm.send(f'Начинаем анкету **{form["name"]}**. Для отмены напишите **отмена**.')
            except discord.Forbidden:
                return await interaction.response.send_message('Откройте личные сообщения от участников сервера и повторите.', ephemeral=True)
            await bot.db.execute('INSERT INTO sessions(user_id,guild_id,form_id) VALUES(?,?,?)', (interaction.user.id, interaction.guild.id, form_id))
            await bot.db.commit()
            session = await one('SELECT * FROM sessions WHERE user_id=?', (interaction.user.id,))
            await send_question(interaction.user, session)
        await interaction.response.send_message('Первый вопрос отправлен вам в личные сообщения.', ephemeral=True)


async def decide(interaction, app_id, status, reason=None):
    if not interaction.guild or not interaction.message:
        return await interaction.response.send_message('Недоступно.', ephemeral=True)
    async with bot.review_lock:
        app = await one('SELECT * FROM applications WHERE id=? AND guild_id=? AND message_id=?',
                        (app_id, interaction.guild.id, interaction.message.id))
        if not app:
            return await interaction.response.send_message('Заявка не найдена.', ephemeral=True)
        form = await one('SELECT * FROM forms WHERE id=?', (app['form_id'],))
        if not form or not admin_ok(interaction.user, form):
            return await interaction.response.send_message('Нет прав администратора.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cur = await bot.db.execute('UPDATE applications SET status=?,reviewer_id=?,reason=? WHERE id=? AND status=?',
                                   (status, interaction.user.id, reason, app_id, 'pending'))
        await bot.db.commit()
        if cur.rowcount != 1:
            return await interaction.followup.send('Заявка уже рассмотрена.', ephemeral=True)
        try:
            await interaction.message.edit(content=f'**{app_id}. <@{app["user_id"]}> отправил(а) форму «{form["name"]}»**\nСтатус: {"принята" if status == "approved" else "отклонена"}\nРешение: <@{interaction.user.id}>' + (f'\nПричина: {reason}' if reason else ''), view=None)
        except discord.HTTPException:
            log.exception('Cannot update application #%s', app_id)
        note = ''
        if status == 'approved' and form['approved_role_id']:
            role = interaction.guild.get_role(form['approved_role_id'])
            member = interaction.guild.get_member(app['user_id'])
            if not member:
                try:
                    member = await interaction.guild.fetch_member(app['user_id'])
                except discord.HTTPException:
                    member = None
            if role and member:
                try:
                    await member.add_roles(role, reason=f'Application #{app_id}')
                except discord.HTTPException:
                    note = ' Не удалось выдать роль.'
        try:
            user = await bot.fetch_user(app['user_id'])
            await user.send(f'Ваша заявка №{app_id} {"одобрена" if status == "approved" else "отклонена"}.' + (f' Причина: {reason}' if reason else ''))
        except discord.HTTPException:
            pass
        await interaction.followup.send('Решение сохранено.' + note, ephemeral=True)


class RejectModal(discord.ui.Modal, title='Причина отказа'):
    reason = discord.ui.TextInput(label='Причина', style=discord.TextStyle.paragraph, max_length=500)
    def __init__(self, app_id):
        super().__init__()
        self.app_id = app_id
    async def on_submit(self, interaction):
        await decide(interaction, self.app_id, 'rejected', str(self.reason))


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def get_app(self, interaction):
        if not interaction.guild or not interaction.message:
            await interaction.response.send_message('Недоступно.', ephemeral=True)
            return None
        app = await one('SELECT * FROM applications WHERE guild_id=? AND message_id=?',
                        (interaction.guild.id, interaction.message.id))
        if not app:
            await interaction.response.send_message('Заявка не найдена.', ephemeral=True)
            return None
        form = await one('SELECT * FROM forms WHERE id=?', (app['form_id'],))
        if not form or not admin_ok(interaction.user, form):
            await interaction.response.send_message('Нет прав администратора.', ephemeral=True)
            return None
        if app['status'] != 'pending':
            await interaction.response.send_message('Заявка уже рассмотрена.', ephemeral=True)
            return None
        return app

    @discord.ui.button(label='Принять', style=discord.ButtonStyle.success, custom_id='plam:approve:v2')
    async def approve(self, interaction, button):
        app = await self.get_app(interaction)
        if app:
            await decide(interaction, app['id'], 'approved')

    @discord.ui.button(label='Отклонить', style=discord.ButtonStyle.danger, custom_id='plam:reject:v2')
    async def reject(self, interaction, button):
        app = await self.get_app(interaction)
        if app:
            await interaction.response.send_modal(RejectModal(app['id']))


@bot.tree.command(name='admin_form_create', description='Создать анкету и назначить роль администраторов')
async def form_create(interaction: discord.Interaction, name: str, review_channel: discord.TextChannel,
                      admin_role: discord.Role, approved_role: discord.Role | None = None):
    if not interaction.guild or not (interaction.user.id == interaction.guild.owner_id or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message('Требуется управление сервером.', ephemeral=True)
    if not 1 <= len(name) <= 50:
        return await interaction.response.send_message('Название: 1–50 символов.', ephemeral=True)
    try:
        await bot.db.execute('INSERT INTO forms(guild_id,name,channel_id,admin_role_id,approved_role_id) VALUES(?,?,?,?,?)',
                             (interaction.guild.id, name, review_channel.id, admin_role.id, approved_role.id if approved_role else None))
        await bot.db.commit()
    except aiosqlite.IntegrityError:
        return await interaction.response.send_message('Форма с таким именем уже есть.', ephemeral=True)
    await interaction.response.send_message('Форма создана. Добавьте вопросы через `/admin_question_add`, затем `/admin_form_publish`.', ephemeral=True)


@bot.tree.command(name='admin_question_add', description='Добавить вопрос без общего лимита количества')
async def question_add(interaction: discord.Interaction, form_name: str, text: str, kind: str, options: str = ''):
    form = await admin_form(interaction, form_name)
    if not form:
        return
    if kind not in ('text', 'choice'):
        return await interaction.response.send_message('Тип: text (свободный ответ) или choice (выбор).', ephemeral=True)
    choices = parse_options(options)
    if kind == 'choice' and not 2 <= len(choices) <= 25:
        return await interaction.response.send_message('Для choice укажите 2–25 вариантов через |.', ephemeral=True)
    if not text.strip() or len(text) > 1000 or any(len(x) > 200 for x in choices):
        return await interaction.response.send_message('Вопрос до 1000 символов, вариант до 200.', ephemeral=True)
    row = await one('SELECT COALESCE(MAX(position),0) AS n FROM questions WHERE form_id=?', (form['id'],))
    await bot.db.execute('INSERT INTO questions(form_id,position,text,kind,options_json) VALUES(?,?,?,?,?)',
                         (form['id'], row['n']+1, text, kind, json.dumps(choices, ensure_ascii=False)))
    await bot.db.commit()
    await interaction.response.send_message(f'Добавлен вопрос №{row["n"]+1}.', ephemeral=True)


@bot.tree.command(name='admin_question_edit', description='Изменить вопрос по номеру')
async def question_edit(interaction: discord.Interaction, form_name: str, number: int, text: str, kind: str, options: str = ''):
    form = await admin_form(interaction, form_name)
    if not form:
        return
    qs = await all_rows('SELECT * FROM questions WHERE form_id=? ORDER BY position,id', (form['id'],))
    if not 1 <= number <= len(qs) or kind not in ('text','choice'):
        return await interaction.response.send_message('Неверный номер или тип.', ephemeral=True)
    choices = parse_options(options)
    if (kind == 'choice' and not 2 <= len(choices) <= 25) or not text.strip() or len(text)>1000 or any(len(x)>200 for x in choices):
        return await interaction.response.send_message('Неверные параметры вопроса.', ephemeral=True)
    await bot.db.execute('UPDATE questions SET text=?,kind=?,options_json=? WHERE id=?',
                         (text, kind, json.dumps(choices, ensure_ascii=False), qs[number-1]['id']))
    await bot.db.commit()
    await interaction.response.send_message('Вопрос обновлён. Активные анкеты продолжат работу по обновлённому списку.', ephemeral=True)


@bot.tree.command(name='admin_question_delete', description='Удалить вопрос по номеру')
async def question_delete(interaction: discord.Interaction, form_name: str, number: int):
    form = await admin_form(interaction, form_name)
    if not form:
        return
    qs = await all_rows('SELECT * FROM questions WHERE form_id=? ORDER BY position,id', (form['id'],))
    if not 1 <= number <= len(qs):
        return await interaction.response.send_message('Неверный номер.', ephemeral=True)
    await bot.db.execute('DELETE FROM questions WHERE id=?', (qs[number-1]['id'],))
    for i, q in enumerate((q for j,q in enumerate(qs) if j != number-1), 1):
        await bot.db.execute('UPDATE questions SET position=? WHERE id=?', (i, q['id']))
    await bot.db.commit()
    await interaction.response.send_message('Вопрос удалён.', ephemeral=True)


@bot.tree.command(name='admin_question_list', description='Показать вопросы формы')
async def question_list(interaction: discord.Interaction, form_name: str):
    form = await admin_form(interaction, form_name)
    if not form:
        return
    qs = await all_rows('SELECT * FROM questions WHERE form_id=? ORDER BY position,id', (form['id'],))
    lines = [f'{i}. [{q["kind"]}] {q["text"]}' for i,q in enumerate(qs,1)]
    await interaction.response.defer(ephemeral=True)
    for chunk in split_text('\n'.join(lines) or 'Вопросов пока нет.'):
        await interaction.followup.send(chunk, ephemeral=True)


@bot.tree.command(name='admin_form_publish', description='Разместить кнопку анкеты в канале')
async def form_publish(interaction: discord.Interaction, form_name: str, channel: discord.TextChannel):
    form = await admin_form(interaction, form_name)
    if not form:
        return
    embed = discord.Embed(title=f'Анкета: {form["name"]}', description='Нажмите кнопку, чтобы получить вопросы в личные сообщения.', color=discord.Color.blurple())
    embed.set_footer(text=f'plam-form:{form["id"]}')
    try:
        await channel.send(embed=embed, view=ApplyView())
    except discord.HTTPException:
        return await interaction.response.send_message('Не удалось опубликовать панель. Проверьте права бота.', ephemeral=True)
    await interaction.response.send_message('Панель опубликована.', ephemeral=True)


@bot.tree.command(name='forms', description='Список анкет сервера')
async def form_list(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message('Только на сервере.', ephemeral=True)
    forms = await all_rows('SELECT name FROM forms WHERE guild_id=? ORDER BY name', (interaction.guild.id,))
    await interaction.response.send_message('Формы: '+(', '.join(f['name'] for f in forms) if forms else 'нет'), ephemeral=True)


@bot.tree.command(name='admin_application_delete', description='Удалить запись заявки и её основное сообщение')
async def application_delete(interaction: discord.Interaction, application_id: int):
    if not interaction.guild:
        return await interaction.response.send_message('Только на сервере.', ephemeral=True)
    app = await one('SELECT * FROM applications WHERE id=? AND guild_id=?', (application_id, interaction.guild.id))
    form = await one('SELECT * FROM forms WHERE id=?', (app['form_id'],)) if app else None
    if not form or not admin_ok(interaction.user, form):
        return await interaction.response.send_message('Заявка не найдена или недостаточно прав.', ephemeral=True)
    channel = bot.get_channel(form['channel_id'])
    if channel and app['message_id']:
        try:
            msg = await channel.fetch_message(app['message_id'])
            await msg.delete()
        except discord.HTTPException:
            pass
    await bot.db.execute('DELETE FROM applications WHERE id=?', (application_id,))
    await bot.db.commit()
    await interaction.response.send_message('Основное сообщение и запись заявки удалены. Сообщения с ответами остаются в канале.', ephemeral=True)


@bot.tree.command(name='admin_application_edit', description='Изменить ответ в базе и добавить исправление в канал')
async def application_edit(interaction: discord.Interaction, application_id: int, answer_number: int, new_answer: str):
    if not interaction.guild:
        return await interaction.response.send_message('Только на сервере.', ephemeral=True)
    app = await one('SELECT * FROM applications WHERE id=? AND guild_id=?', (application_id, interaction.guild.id))
    form = await one('SELECT * FROM forms WHERE id=?', (app['form_id'],)) if app else None
    if not form or not admin_ok(interaction.user, form):
        return await interaction.response.send_message('Заявка не найдена или недостаточно прав.', ephemeral=True)
    answers = json.loads(app['answers_json'])
    if not 1 <= answer_number <= len(answers) or not new_answer.strip():
        return await interaction.response.send_message('Неверный номер ответа или пустой текст.', ephemeral=True)
    answers[answer_number-1]['answer'] = new_answer
    await bot.db.execute('UPDATE applications SET answers_json=? WHERE id=?', (json.dumps(answers, ensure_ascii=False), application_id))
    await bot.db.commit()
    channel = bot.get_channel(form['channel_id'])
    if channel:
        for chunk in split_text(f'**Исправление заявки №{application_id}, ответ {answer_number}** (администратор <@{interaction.user.id}>):\n{new_answer}'):
            await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
    await interaction.response.send_message('Ответ исправлен в базе, изменение опубликовано в канале.', ephemeral=True)




@bot.tree.command(name='my_applications', description='Посмотреть статусы своих заявок')
async def my_applications(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message('Команда доступна только на сервере.', ephemeral=True)
    rows = await all_rows('''SELECT a.id, a.status, a.created_at, f.name
        FROM applications a JOIN forms f ON f.id=a.form_id
        WHERE a.guild_id=? AND a.user_id=? ORDER BY a.id DESC LIMIT 15''',
        (interaction.guild.id, interaction.user.id))
    if not rows:
        return await interaction.response.send_message('У вас пока нет заявок.', ephemeral=True)
    names = {'pending':'на рассмотрении','approved':'принята','rejected':'отклонена'}
    lines = [f'№{r["id"]} — {discord.utils.escape_markdown(r["name"])} — {names.get(r["status"],r["status"])}' for r in rows]
    await interaction.response.send_message('\n'.join(lines)[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='cancel_application', description='Отменить заполнение текущей анкеты')
async def cancel_application(interaction: discord.Interaction):
    lock = bot.user_locks.setdefault(interaction.user.id, asyncio.Lock())
    async with lock:
        row = await one('SELECT user_id FROM sessions WHERE user_id=?', (interaction.user.id,))
        if not row:
            return await interaction.response.send_message('У вас нет незавершённой анкеты.', ephemeral=True)
        await bot.db.execute('DELETE FROM sessions WHERE user_id=?', (interaction.user.id,))
        await bot.db.commit()
    await interaction.response.send_message('Заполнение анкеты отменено.', ephemeral=True)


@bot.event
async def on_ready():
    log.info('Online as %s', bot.user)


if __name__ == '__main__':
    if not TOKEN:
        raise SystemExit('Set DISCORD_TOKEN')
    bot.run(TOKEN)
