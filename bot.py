import discord
import asyncio
import re
import os
import math
import random
import aiosqlite
from datetime import datetime, timedelta
from dotenv import load_dotenv
from discord.ext import commands

# ====================== LOAD TOKEN ======================
load_dotenv()
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise Exception("No TOKEN found in environment variables!")

# ====================== DATABASE SETUP ======================
DB_NAME = "levels.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS levels (
                user_id INTEGER PRIMARY KEY,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1
            )
        """)
        await db.commit()

async def get_user_level(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT xp, level FROM levels WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await db.execute(
                "INSERT INTO levels (user_id, xp, level) VALUES (?, 0, 1)",
                (user_id,)
            )
            await db.commit()
            return 0, 1

        return row

async def add_xp(user_id: int, amount: int):
    xp, level = await get_user_level(user_id)
    new_xp = xp + amount
    new_level = int(math.sqrt(new_xp / 100)) + 1

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO levels (user_id, xp, level) VALUES (?, ?, ?)",
            (user_id, new_xp, new_level)
        )
        await db.commit()

    return new_xp, new_level, new_level > level

# ====================== NUMBER FORMATTER & PARSER ======================
def format_game_number(num: float) -> str:
    if num == 0:
        return "0"
    suffixes = [('', 1), ('K', 1e3), ('M', 1e6), ('B', 1e9), ('T', 1e12),
                ('Qa', 1e15), ('Qi', 1e18), ('Sx', 1e21), ('Sp', 1e24),
                ('Oc', 1e27), ('No', 1e30)]
    for suffix, value in reversed(suffixes):
        if abs(num) >= value:
            formatted = num / value
            return f"{int(formatted)}{suffix}" if formatted.is_integer() else f"{formatted:.2f}{suffix}"
    return f"{num:.2f}"

def parse_game_number(s: str) -> float:
    if not s:
        raise ValueError("Empty input")
    s = s.strip().upper().replace(" ", "").replace(",", "")
    s = re.sub(r'S$', '', s)
    match = re.match(r'^([0-9.]+)([A-Z]*)?$', s)
    if not match:
        try:
            return float(s)
        except ValueError:
            raise ValueError(f"Invalid format: {s}")
    num_str, suffix = match.groups()
    num = float(num_str)
    multipliers = {'': 1, 'K': 1e3, 'M': 1e6, 'B': 1e9, 'T': 1e12,
                   'QA': 1e15, 'QI': 1e18, 'SX': 1e21, 'SP': 1e24,
                   'OC': 1e27, 'NO': 1e30}
    if suffix and suffix not in multipliers:
        raise ValueError(f"Unknown suffix '{suffix}'.")
    return num * multipliers.get(suffix, 1)

def progress_bar(current, total, length=10):
    filled = int(length * current / total) if total > 0 else 0
    return "█" * filled + "░" * (length - filled)

# ====================== BOT SETUP ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='.', intents=intents, help_command=None)

last_xp_time = {}   # Anti-spam cooldown

ROLE_XP_MULTIPLIERS = {
    "Viltrumite": 1.5,
    "Elite": 2.0,
    "Veteran Viltrumite": 2.5
}

ROLE_PRIORITY = {
    "Veteran Viltrumite": 3,
    "Elite": 2,
    "Viltrumite": 1
}

@bot.event
async def on_ready():
    await init_db()
    print(f'✅ Bot is online as {bot.user}')

# ====================== LEVELING SYSTEM ======================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Skip XP in #commands channel
    if message.channel.name.lower() == "commands":
        await bot.process_commands(message)
        return

    # 60-second cooldown
    now = datetime.utcnow()
    if message.author.id in last_xp_time and now - last_xp_time[message.author.id] < timedelta(seconds=60):
        await bot.process_commands(message)
        return

    last_xp_time[message.author.id] = now

    xp_gain = random.randint(10, 25)
    member = message.author
    best_role = None
    best_priority = 0

    for role in member.roles:
        if role.name in ROLE_PRIORITY:
            if ROLE_PRIORITY[role.name] > best_priority:
                best_priority = ROLE_PRIORITY[role.name]
                best_role = role.name

    multiplier = ROLE_XP_MULTIPLIERS.get(best_role, 1.0)

    xp_gain = int(xp_gain * multiplier)

    new_xp, new_level, leveled_up = await add_xp(message.author.id, xp_gain)

    if leveled_up:
        # Send level-up message ONLY in #level-up channel
        level_up_channel = discord.utils.get(message.guild.text_channels, name="level-up")
        if level_up_channel:
            embed = discord.Embed(
                title="🎉 Level Up!",
                description=f"{message.author.mention} has reached **Level {new_level}**!",
                color=0x00ff88
            )
            embed.add_field(name="Total XP", value=f"{new_xp:,}", inline=True)
        await level_up_channel.send(embed=embed)

    await bot.process_commands(message)

# ====================== HELPER: Commands Channel Check ======================
def is_commands_channel(ctx):
    return ctx.channel.name.lower() == "commands"

# ====================== HELP COMMAND ======================
@bot.command(name='help')
async def help_command(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return

    embed = discord.Embed(
        title="🤖 Viltrumite Bot",
        description="Power, Token & Leveling system",
        color=0x00ff88
    )
    embed.add_field(
        name="📋 Available Commands",
        value="`.pcalculate` - Power time calculator\n"
              "`.tcalculate` - Token time calculator\n"
              "`.rank` - Show your current level & progress\n"
              "`.leaderboard` - Top 10 users on the server",
        inline=False
    )
    embed.set_footer(text="Leveling works by chatting | Level-ups appear in #level-up")
    await ctx.send(embed=embed)

# ====================== RANK COMMAND ======================
# ====================== RANK COMMAND ======================
@bot.command(name='rank')
async def rank(ctx):
    xp, level = await get_user_level(ctx.author.id)   # ← This line was missing
    
    current_level_xp = ((level - 1) ** 2) * 100
    next_level_xp = (level ** 2) * 100

    xp_into_level = xp - current_level_xp
    xp_needed_level = next_level_xp - current_level_xp

    progress = (xp_into_level / xp_needed_level) * 100 if xp_needed_level > 0 else 100
    xp_needed = max(0, xp_needed_level - xp_into_level)

    embed = discord.Embed(title=f"{ctx.author.display_name}'s Rank", color=0x00ff88)
    embed.add_field(name="Level", value=f"**{level}**", inline=True)
    embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
    embed.add_field(name="Progress", value=f"{progress:.1f}%", inline=True)
    embed.add_field(name="Progress Bar", value=progress_bar(xp_into_level, xp_needed_level), inline=False)
    embed.add_field(name="XP to next level", value=f"{next_level_xp - xp:,}", inline=False)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

# ====================== LEADERBOARD COMMAND ======================
@bot.command(name='leaderboard')
async def leaderboard(ctx):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, xp, level FROM levels ORDER BY xp DESC LIMIT 10") as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await ctx.send("No users have XP yet!")
        return

    embed = discord.Embed(title="🏆 Server Leaderboard", color=0xFFD700)
    desc = ""
    for i, (user_id, xp, level) in enumerate(rows, 1):
        member = ctx.guild.get_member(user_id)
        name = member.display_name if member else f"User {user_id}"
        desc += f"**#{i}** {name} — Level **{level}** ({xp:,} XP)\n"
    
    embed.description = desc
    await ctx.send(embed=embed)

# ====================== YOUR ORIGINAL COMMANDS ======================
# Paste your full .pcalculate and .tcalculate here (unchanged)

@bot.command(name='pcalculate')
async def pcalculate(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return
    await ctx.send("🔢 **Power Calculator started!**\n\n"
                   "**1.** What is your **current power**?\n"
                   "Example: `19.12T`, `5Qa`, `100Sx`, or just a number")

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg = await bot.wait_for('message', check=check, timeout=180)
        current = parse_game_number(msg.content)

        await ctx.send("**2.** What is your **power gain per tick**?\n"
                       "Example: `1.5Qa`, `25B`, `500T`")

        msg = await bot.wait_for('message', check=check, timeout=180)
        gain_per_tick = parse_game_number(msg.content)

        await ctx.send("**3.** What is your **tick rate** in seconds?\n"
                       "Example: `0.264` or `0.264s`")

        msg = await bot.wait_for('message', check=check, timeout=180)
        tick_rate = float(msg.content.strip().lower().replace("s", "").replace(" ", ""))

        await ctx.send("**4.** What is your **goal power**?\n"
                       "Example: `10Qa`, `100Sx`, `1.5Qi`")

        msg = await bot.wait_for('message', check=check, timeout=180)
        goal = parse_game_number(msg.content)

        if goal <= current:
            await ctx.send("🎉 You have already reached or passed your goal!")
            return

        if gain_per_tick <= 0 or tick_rate <= 0:
            await ctx.send("❌ Gain per tick and tick rate must be greater than 0!")
            return

        needed = goal - current
        ticks_needed = math.ceil(needed / gain_per_tick)
        total_seconds = ticks_needed * tick_rate

        if total_seconds < 60:
            time_str = f"{total_seconds:.1f} seconds"
        elif total_seconds < 3600:
            time_str = f"{total_seconds/60:.2f} minutes"
        elif total_seconds < 86400:
            time_str = f"{total_seconds/3600:.2f} hours"
        else:
            time_str = f"{total_seconds/86400:.2f} days"

        embed = discord.Embed(title="⏳ Time to Reach Power Goal", color=0x00ff88)
        embed.add_field(name="Current Power", value=format_game_number(current), inline=True)
        embed.add_field(name="Gain per Tick", value=format_game_number(gain_per_tick), inline=True)
        embed.add_field(name="Tick Rate", value=f"{tick_rate} s", inline=True)
        embed.add_field(name="Goal Power", value=format_game_number(goal), inline=True)
        embed.add_field(name="Ticks Needed", value=f"{ticks_needed:,}", inline=False)
        embed.add_field(name="Estimated Time", value=f"**{time_str}**", inline=False)

        await ctx.send(embed=embed)

    except asyncio.TimeoutError:
        await ctx.send("⏰ You took too long to reply. Type `.pcalculate` again.")
    except ValueError as e:
        await ctx.send(f"❌ Invalid number format: {e}\nPlease try `.pcalculate` again.")
    except Exception:
        await ctx.send("❌ Something went wrong.")

@bot.command(name='tcalculate')
async def tcalculate(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return
    await ctx.send("🔢 **Token Calculator started!**\n\n"
                   "**1.** How many **tokens do you earn per tick**?\n"
                   "Example: `150`, `2500`, `25162`")

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg = await bot.wait_for('message', check=check, timeout=180)
        tokens_per_tick = parse_game_number(msg.content)

        await ctx.send("**2.** What is your **tick rate** (speed)?\n"
                       "Example: `32s`, `60`, `35.5s`")

        msg = await bot.wait_for('message', check=check, timeout=180)
        tick_rate = float(msg.content.strip().lower().replace("s", "").replace(" ", ""))

        await ctx.send("**3.** How many **tokens do you need** (goal)?\n"
                       "Example: `50000`, `605K`, `32.5M`")

        msg = await bot.wait_for('message', check=check, timeout=180)
        goal = parse_game_number(msg.content)

        if goal <= tokens_per_tick:
            await ctx.send("🎉 You can already reach your token goal in 1 tick!")
            return

        if tokens_per_tick <= 0 or tick_rate <= 0:
            await ctx.send("❌ Tokens per tick and tick rate must be greater than 0!")
            return

        needed = goal 
        ticks_needed = math.ceil(needed / tokens_per_tick)
        total_seconds = ticks_needed * tick_rate

        if total_seconds < 60:
            time_str = f"{total_seconds:.1f} seconds"
        elif total_seconds < 3600:
            time_str = f"{total_seconds/60:.2f} minutes"
        elif total_seconds < 86400:
            time_str = f"{total_seconds/3600:.2f} hours"
        else:
            time_str = f"{total_seconds/86400:.2f} days"

        embed = discord.Embed(title="⏳ Time to Reach Token Goal", color=0x0099ff)
        embed.add_field(name="Tokens per Tick", value=format_game_number(tokens_per_tick), inline=True)
        embed.add_field(name="Tick Rate", value=f"{tick_rate} s", inline=True)
        embed.add_field(name="Token Goal", value=format_game_number(goal), inline=True)
        embed.add_field(name="Ticks Needed", value=f"{ticks_needed:,}", inline=False)
        embed.add_field(name="Estimated Time", value=f"**{time_str}**", inline=False)

        await ctx.send(embed=embed)

    except asyncio.TimeoutError:
        await ctx.send("⏰ You took too long to reply. Type `.tcalculate` again.")
    except ValueError as e:
        await ctx.send(f"❌ Invalid number format: {e}\nPlease try `.tcalculate` again.")
    except Exception:
        await ctx.send("❌ Something went wrong.")
# ====================== RUN BOT ======================
bot.run(TOKEN)