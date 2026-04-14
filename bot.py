import discord
from discord.ext import commands, tasks
import asyncio
import os
import json
import time
import aiohttp
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from collections import defaultdict
from datetime import datetime, timedelta
import urllib.parse
import io

# ============== КОНФИГУРАЦИЯ ДЛЯ RENDER ==============
BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
GUILD_ID = int(os.environ.get('DISCORD_GUILD_ID', 0))
PORT = int(os.environ.get('PORT', 10000))
SITE_URL = os.environ.get('SITE_URL', 'https://koshak-nelud-github-io.onrender.com')

print(f"🌐 Сайт: {SITE_URL}")
print(f"🔌 Порт: {PORT}")

# ID каналов для уведомлений
REVIEWS_CHANNEL_ID = os.environ.get('REVIEWS_CHANNEL_ID')
COMPLAINTS_CHANNEL_ID = os.environ.get('COMPLAINTS_CHANNEL_ID')

if REVIEWS_CHANNEL_ID:
    REVIEWS_CHANNEL_ID = int(REVIEWS_CHANNEL_ID)
if COMPLAINTS_CHANNEL_ID:
    COMPLAINTS_CHANNEL_ID = int(COMPLAINTS_CHANNEL_ID)

# Парсим роли
REVIEWER_ROLES = [r.strip() for r in os.environ.get('REVIEWER_ROLES', '').split(',') if r.strip()]
MODERATOR_ROLES = [r.strip() for r in os.environ.get('MODERATOR_ROLES', '').split(',') if r.strip()]
SUPPORTER_ROLES = [r.strip() for r in os.environ.get('SUPPORTER_ROLES', '').split(',') if r.strip()]

# Статистика пользователей
user_messages = defaultdict(int)
user_voice_time = defaultdict(int)
user_voice_start = {}
user_avatars = {}

print(f"\n{'='*50}")
print(f"📋 Загруженные настройки:")
print(f"   REVIEWER_ROLES: {REVIEWER_ROLES}")
print(f"   MODERATOR_ROLES: {MODERATOR_ROLES}")
print(f"   SUPPORTER_ROLES: {SUPPORTER_ROLES}")
print(f"   REVIEWS_CHANNEL_ID: {REVIEWS_CHANNEL_ID}")
print(f"   COMPLAINTS_CHANNEL_ID: {COMPLAINTS_CHANNEL_ID}")
print(f"{'='*50}\n")

# Создаем бота
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
intents.voice_states = True
intents.presences = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Глобальные переменные
server_stats = {
    'total_members': 0,
    'online_members': 0,
    'voice_members': 0
}

supporters_cache = []
last_supporters_update = 0

# ============== HTTP-СЕРВЕР ДЛЯ RENDER HEALTH CHECK ==============
class APIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        if path == '/health' or path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            guild = bot.get_guild(GUILD_ID)
            response = {
                'status': 'ok',
                'bot_ready': bot.is_ready(),
                'bot_name': bot.user.name if bot.user else None,
                'guild_name': guild.name if guild else None,
                'guild_id': GUILD_ID
            }
            self.wfile.write(json.dumps(response).encode())
            
        elif path == '/stats':
            update_server_stats()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'server': server_stats}).encode())
            
        elif path == '/supporters':
            supporters = get_supporters_from_roles()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'supporters': supporters}).encode())
            
        elif path == '/guild-info':
            guild = bot.get_guild(GUILD_ID)
            if guild:
                icon_url = guild.icon.url if guild.icon else 'https://cdn.discordapp.com/embed/avatars/0.png'
                response = {
                    'success': True,
                    'name': guild.name,
                    'icon_url': icon_url,
                    'member_count': guild.member_count,
                    'id': str(guild.id)
                }
                self.send_response(200)
            else:
                response = {'error': 'Сервер не найден'}
                self.send_response(404)
            
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
            
        elif path == '/top-messages':
            limit = int(parsed.query.split('=')[1]) if 'limit=' in parsed.query else 10
            sorted_users = sorted(user_messages.items(), key=lambda x: x[1], reverse=True)[:limit]
            
            result = []
            for user_id, count in sorted_users:
                avatar_url = user_avatars.get(str(user_id), 'https://cdn.discordapp.com/embed/avatars/0.png')
                result.append({
                    'user_id': str(user_id),
                    'count': count,
                    'avatar_url': avatar_url
                })
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'top': result}).encode())
            
        elif path == '/top-voice':
            limit = int(parsed.query.split('=')[1]) if 'limit=' in parsed.query else 10
            sorted_users = sorted(user_voice_time.items(), key=lambda x: x[1], reverse=True)[:limit]
            
            result = []
            for user_id, seconds in sorted_users:
                avatar_url = user_avatars.get(str(user_id), 'https://cdn.discordapp.com/embed/avatars/0.png')
                result.append({
                    'user_id': str(user_id),
                    'seconds': seconds,
                    'hours': seconds // 3600,
                    'minutes': (seconds % 3600) // 60,
                    'avatar_url': avatar_url
                })
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'top': result}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        
        try:
            data = json.loads(body) if body else {}
        except:
            data = {}
        
        path = urllib.parse.urlparse(self.path).path
        
        if path == '/role-members':
            role_id = data.get('role_id')
            if not role_id:
                response = {'error': 'role_id обязателен'}
                self.send_response(400)
            else:
                future = asyncio.run_coroutine_threadsafe(get_members_by_role(role_id), bot.loop)
                try:
                    members = future.result(timeout=10)
                    response = {'success': True, 'members': members}
                    self.send_response(200)
                except Exception as e:
                    response = {'error': str(e)}
                    self.send_response(500)
                    
        elif path == '/check-role':
            if not bot.is_ready():
                response = {'error': 'Бот не готов'}
                self.send_response(503)
            else:
                user_id = data.get('user_id')
                if not user_id:
                    response = {'error': 'user_id обязателен'}
                    self.send_response(400)
                else:
                    future = asyncio.run_coroutine_threadsafe(check_user_roles_async(user_id), bot.loop)
                    try:
                        response = future.result(timeout=10)
                        self.send_response(200)
                    except asyncio.TimeoutError:
                        response = {'error': 'Timeout'}
                        self.send_response(504)
                    except Exception as e:
                        response = {'error': str(e)}
                        self.send_response(500)
                        
        elif path == '/notify-review':
            print(f"📨 Получено уведомление об отзыве: {data}")
            future = asyncio.run_coroutine_threadsafe(send_review_notification(data), bot.loop)
            try:
                future.result(timeout=5)
                response = {'success': True}
                self.send_response(200)
            except Exception as e:
                print(f"Ошибка отправки уведомления: {e}")
                response = {'error': str(e)}
                self.send_response(500)
                
        elif path == '/notify-complaint':
            print(f"📨 Получено уведомление о жалобе: {data}")
            future = asyncio.run_coroutine_threadsafe(send_complaint_notification(data), bot.loop)
            try:
                future.result(timeout=10)
                response = {'success': True}
                self.send_response(200)
            except Exception as e:
                print(f"Ошибка отправки уведомления: {e}")
                response = {'error': str(e)}
                self.send_response(500)
        else:
            response = {'error': 'Not found'}
            self.send_response(404)
        
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())
    
    def log_message(self, format, *args):
        # Отключаем логи HTTP-запросов
        pass

def run_http_server():
    server = HTTPServer(('0.0.0.0', PORT), APIHandler)
    print(f"🏥 HTTP сервер запущен на порту {PORT}")
    server.serve_forever()

# ============== ФУНКЦИИ БОТА (ВСЕ ОСТАЛЬНЫЕ ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ) ==============

def update_server_stats():
    global server_stats
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    
    server_stats['total_members'] = guild.member_count
    
    online = 0
    voice = 0
    
    for member in guild.members:
        if member.status != discord.Status.offline:
            online += 1
        if member.voice and member.voice.channel:
            voice += 1
    
    server_stats['online_members'] = online
    server_stats['voice_members'] = voice

def get_supporters_from_roles():
    global supporters_cache, last_supporters_update
    
    if time.time() - last_supporters_update < 300 and supporters_cache:
        return supporters_cache
    
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return []
    
    supporters = []
    supporter_ids = set()
    
    for role_id in SUPPORTER_ROLES:
        try:
            role = guild.get_role(int(role_id))
            if role:
                for member in role.members:
                    if member.id not in supporter_ids:
                        supporter_ids.add(member.id)
                        avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
                        supporters.append({
                            'id': len(supporters) + 1,
                            'discordId': str(member.id),
                            'username': member.name,
                            'display_name': member.display_name,
                            'role': role.name,
                            'avatar_url': avatar_url
                        })
                print(f"   ✅ Загружено {len(role.members)} из роли {role.name}")
            else:
                print(f"   ❌ Роль {role_id} не найдена!")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
    
    supporters_cache = supporters
    last_supporters_update = time.time()
    return supporters

async def send_review_notification(review_data):
    """Отправляет уведомление о новом отзыве в канал"""
    if not REVIEWS_CHANNEL_ID:
        print("⚠️ REVIEWS_CHANNEL_ID не указан, уведомление не отправлено")
        return
    
    channel = bot.get_channel(REVIEWS_CHANNEL_ID)
    if not channel:
        print(f"❌ Канал с ID {REVIEWS_CHANNEL_ID} не найден")
        return
    
    print(f"📝 Отправка уведомления об отзыве в канал {channel.name}")
    
    embed = discord.Embed(
        title="⭐ Новый отзыв!",
        description=f"Пользователь **{review_data['username']}** оставил отзыв",
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    
    supporter = None
    for s in get_supporters_from_roles():
        if s['id'] == review_data['supporterId']:
            supporter = s
            break
    
    embed.add_field(name="🎮 Саппорт", value=f"**{supporter['username']}** ({supporter['role']})" if supporter else f"ID: {review_data['supporterId']}", inline=True)
    embed.add_field(name="⭐ Оценка", value=f"{'⭐' * review_data['rating']} {review_data['rating']}/5", inline=True)
    
    if review_data.get('comment'):
        embed.add_field(name="💬 Комментарий", value=review_data['comment'][:500], inline=False)
    
    embed.set_footer(text=f"ID пользователя: {review_data['userId']}")
    
    content = ""
    if SUPPORTER_ROLES:
        mentions = []
        for role_id in SUPPORTER_ROLES:
            role = channel.guild.get_role(int(role_id))
            if role:
                mentions.append(role.mention)
        if mentions:
            content = " ".join(mentions)
    
    await channel.send(content=content, embed=embed)
    print(f"✅ Уведомление об отзыве отправлено в канал {channel.name}")

async def send_complaint_notification(complaint_data):
    """Отправляет уведомление о новой жалобе в канал с видео"""
    if not COMPLAINTS_CHANNEL_ID:
        print("⚠️ COMPLAINTS_CHANNEL_ID не указан, уведомление не отправлено")
        return
    
    channel = bot.get_channel(COMPLAINTS_CHANNEL_ID)
    if not channel:
        print(f"❌ Канал с ID {COMPLAINTS_CHANNEL_ID} не найден")
        return
    
    print(f"📝 Отправка уведомления о жалобе в канал {channel.name}")
    
    embed = discord.Embed(
        title="📋 Новая жалоба!",
        description=f"Пользователь **{complaint_data['username']}** подал жалобу",
        color=discord.Color.orange(),
        timestamp=datetime.now()
    )
    
    embed.add_field(name="👤 Нарушитель", value=f"**{complaint_data['playerName']}**", inline=True)
    embed.add_field(name="📝 Причина", value=complaint_data['reason'][:500], inline=False)
    embed.set_footer(text=f"ID пользователя: {complaint_data['userId']} | Жалоба #{complaint_data['complaintId']}")
    
    content = ""
    if MODERATOR_ROLES:
        mentions = []
        for role_id in MODERATOR_ROLES:
            role = channel.guild.get_role(int(role_id))
            if role:
                mentions.append(role.mention)
        if mentions:
            content = " ".join(mentions)
    
    await channel.send(content=content, embed=embed)
    
    video_path = complaint_data.get('videoPath')
    if video_path:
        full_video_path = os.path.join(os.path.dirname(__file__), '..', video_path.lstrip('/'))
        
        if os.path.exists(full_video_path):
            try:
                with open(full_video_path, 'rb') as video_file:
                    video = discord.File(video_file, filename=os.path.basename(full_video_path))
                    await channel.send(content="🎬 **Видеодоказательство:**", file=video)
                print(f"✅ Видео отправлено в канал {channel.name}")
            except Exception as e:
                print(f"❌ Ошибка отправки видео: {e}")
                await channel.send(content=f"⚠️ Не удалось отправить видео: {e}")
        else:
            print(f"❌ Видео файл не найден: {full_video_path}")
            await channel.send(content=f"⚠️ Видео файл не найден: {video_path}")
    
    print(f"✅ Уведомление о жалобе отправлено в канал {channel.name}")

async def get_members_by_role(role_id):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return []
    
    role = guild.get_role(int(role_id))
    if not role:
        return []
    
    members = []
    for member in role.members:
        avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
        members.append({
            'id': str(member.id),
            'username': member.name,
            'display_name': member.display_name,
            'avatar_url': avatar_url,
            'joined_at': member.joined_at.isoformat() if member.joined_at else None
        })
    
    return members

async def check_user_roles_async(user_id):
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            guild = await bot.fetch_guild(GUILD_ID)
        
        try:
            member = await guild.fetch_member(int(user_id))
        except:
            return {
                'can_review': False,
                'is_moderator': False,
                'reviewer_roles': [],
                'moderator_roles': [],
                'all_roles': [],
                'username': None,
                'user_id': str(user_id)
            }
        
        reviewer_roles = []
        moderator_roles = []
        all_roles = []
        
        for role in member.roles:
            if role.name != '@everyone':
                all_roles.append({'id': str(role.id), 'name': role.name})
                
                if str(role.id) in REVIEWER_ROLES:
                    reviewer_roles.append({'id': str(role.id), 'name': role.name})
                if str(role.id) in MODERATOR_ROLES:
                    moderator_roles.append({'id': str(role.id), 'name': role.name})
        
        avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
        user_avatars[str(user_id)] = avatar_url
        
        return {
            'can_review': len(reviewer_roles) > 0,
            'is_moderator': len(moderator_roles) > 0,
            'reviewer_roles': reviewer_roles,
            'moderator_roles': moderator_roles,
            'all_roles': all_roles,
            'username': member.name,
            'user_id': str(user_id),
            'avatar': member.avatar.key if member.avatar else None,
            'avatar_url': avatar_url,
            'discriminator': member.discriminator
        }
    except Exception as e:
        print(f"Ошибка: {e}")
        return {
            'can_review': False,
            'is_moderator': False,
            'reviewer_roles': [],
            'moderator_roles': [],
            'all_roles': [],
            'username': None,
            'user_id': str(user_id)
        }

# ============== СБОР СТАТИСТИКИ ==============

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    user_messages[message.author.id] += 1
    
    avatar_url = message.author.avatar.url if message.author.avatar else message.author.default_avatar.url
    user_avatars[str(message.author.id)] = avatar_url
    
    await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is None and after.channel is not None:
        user_voice_start[member.id] = time.time()
        print(f"🔊 {member.name} зашел в голосовой канал {after.channel.name}")
    
    elif before.channel is not None and after.channel is None:
        if member.id in user_voice_start:
            duration = time.time() - user_voice_start[member.id]
            user_voice_time[member.id] += int(duration)
            del user_voice_start[member.id]
            print(f"🔊 {member.name} пробыл в голосовом канале {duration//60} минут")
    
    elif before.channel is not None and after.channel is not None and before.channel != after.channel:
        if member.id in user_voice_start:
            duration = time.time() - user_voice_start[member.id]
            user_voice_time[member.id] += int(duration)
            user_voice_start[member.id] = time.time()
    
    update_server_stats()

@tasks.loop(minutes=5)
async def save_stats():
    print(f"📊 Статистика: {len(user_messages)} пользователей с сообщениями, {len(user_voice_time)} с голосовым временем")

# ============== КОМАНДЫ БОТА ==============

@bot.command(name='myroles')
async def my_roles(ctx):
    roles = [role.name for role in ctx.author.roles if role.name != '@everyone']
    if roles:
        await ctx.send(f"📋 Ваши роли: {', '.join(roles)}")
    else:
        await ctx.send("📋 У вас нет дополнительных ролей")

@bot.command(name='topmessages')
async def top_messages(ctx, limit: int = 10):
    sorted_users = sorted(user_messages.items(), key=lambda x: x[1], reverse=True)[:limit]
    
    if not sorted_users:
        await ctx.send("📊 Нет данных о сообщениях")
        return
    
    result = "📊 **Топ по сообщениям:**\n"
    for i, (user_id, count) in enumerate(sorted_users, 1):
        member = ctx.guild.get_member(user_id)
        name = member.name if member else f"ID: {user_id}"
        result += f"{i}. {name} - {count} сообщений\n"
    
    await ctx.send(result)

@bot.command(name='topvoice')
async def top_voice(ctx, limit: int = 10):
    sorted_users = sorted(user_voice_time.items(), key=lambda x: x[1], reverse=True)[:limit]
    
    if not sorted_users:
        await ctx.send("📊 Нет данных о голосовом времени")
        return
    
    result = "🎤 **Топ по голосовому времени:**\n"
    for i, (user_id, seconds) in enumerate(sorted_users, 1):
        member = ctx.guild.get_member(user_id)
        name = member.name if member else f"ID: {user_id}"
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        result += f"{i}. {name} - {hours}ч {minutes}мин\n"
    
    await ctx.send(result)

@bot.command(name='stats')
async def show_stats(ctx):
    total_messages = sum(user_messages.values())
    total_voice = sum(user_voice_time.values())
    hours = total_voice // 3600
    
    await ctx.send(f"📊 **Статистика сервера:**\n"
                   f"💬 Всего сообщений: {total_messages}\n"
                   f"🎤 Всего голосового времени: {hours} часов")

# ============== СОБЫТИЯ БОТА ==============

@bot.event
async def on_ready():
    print(f'\n{"="*50}')
    print(f'✅ Бот {bot.user.name} запущен!')
    print(f'📡 ID бота: {bot.user.id}')
    
    guild = bot.get_guild(GUILD_ID)
    if guild:
        print(f'🎮 Сервер: {guild.name}')
        print(f'👥 Участников: {guild.member_count}')
        
        if REVIEWS_CHANNEL_ID:
            channel = bot.get_channel(REVIEWS_CHANNEL_ID)
            if channel:
                print(f'📝 Канал отзывов: #{channel.name}')
            else:
                print(f'❌ Канал с ID {REVIEWS_CHANNEL_ID} не найден!')
        
        if COMPLAINTS_CHANNEL_ID:
            channel = bot.get_channel(COMPLAINTS_CHANNEL_ID)
            if channel:
                print(f'📋 Канал жалоб: #{channel.name}')
            else:
                print(f'❌ Канал с ID {COMPLAINTS_CHANNEL_ID} не найден!')
        
        for member in guild.members:
            if member.voice and member.voice.channel:
                user_voice_start[member.id] = time.time()
        
        print(f'\n📋 Роли саппортов:')
        for role_id in SUPPORTER_ROLES:
            role = guild.get_role(int(role_id))
            if role:
                print(f'   ✅ {role.name} - {len(role.members)} участников')
            else:
                print(f'   ❌ Роль {role_id} не найдена!')
        
        update_server_stats()
        get_supporters_from_roles()
        save_stats.start()
    else:
        print(f'❌ Сервер с ID {GUILD_ID} не найден!')
    
    print(f'🔌 API: {SITE_URL}')
    print(f'📝 Команды: !myroles, !topmessages, !topvoice, !stats')
    print(f'{"="*50}\n')

@bot.event
async def on_member_join(member):
    update_server_stats()

@bot.event
async def on_member_remove(member):
    update_server_stats()

# ============== ЗАПУСК ==============
if __name__ == '__main__':
    if not BOT_TOKEN:
        print("❌ Ошибка: DISCORD_BOT_TOKEN не найден!")
        exit(1)
    
    if not GUILD_ID:
        print("❌ Ошибка: DISCORD_GUILD_ID не найден!")
        exit(1)
    
    print("🚀 Запуск бота и HTTP сервера...")
    
    # Запускаем HTTP сервер в отдельном потоке
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()
    
    # Запускаем Discord бота
    try:
        bot.run(BOT_TOKEN)
    except discord.LoginFailure:
        print("❌ Ошибка: Неверный токен бота!")
    except Exception as e:
        print(f"❌ Ошибка: {e}")