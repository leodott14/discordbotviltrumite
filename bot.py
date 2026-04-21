import discord
import asyncio
import re
import os
import math
import random
import aiosqlite
import asyncpg
import random
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from discord.ext import commands

# ====================== LOAD TOKEN ======================
load_dotenv()
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise Exception("No TOKEN found in environment variables!")

# ====================== DATABASE SETUP ======================
db_ready = False

DATABASE_URL = os.getenv("DATABASE_URL")

db_pool = None

async def init_db():
    global db_pool, db_ready

    while True:
        try:
            print("📦 Connecting to PostgreSQL...")

            db_pool = await asyncpg.create_pool(DATABASE_URL)

            async with db_pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS levels (
                        user_id BIGINT PRIMARY KEY,
                        xp BIGINT DEFAULT 0,
                        level INT DEFAULT 1,
                        sigils BIGINT DEFAULT 0,
                        last_daily TEXT
                    )
                """)

            db_ready = True
            print("✅ PostgreSQL ready!")
            return

        except Exception as e:
            print("❌ DB INIT FAILED, retrying in 5s:", e)
            db_pool = None
            db_ready = False
            await asyncio.sleep(5)


async def get_user_level(user_id: int):
    await ensure_db()

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT xp, level, sigils FROM levels WHERE user_id = $1",
            user_id
        )

        if row is None:
            await conn.execute(
                """
                INSERT INTO levels (user_id, xp, level, sigils, last_daily)
                VALUES ($1, 0, 1, 0, NULL)
                """,
                user_id
            )
            return 0, 1, 0

        return row["xp"], row["level"], row["sigils"]
    
async def ensure_db():
    global db_pool, db_ready

    if db_pool is None:
        raise Exception("DB pool not initialized yet")

    if not db_ready:
        raise Exception("DB still initializing")


async def add_xp(user_id: int, amount: int):
    xp, level, sigils = await get_user_level(user_id)

    new_xp = xp + amount
    new_level = int(math.sqrt(new_xp / 100)) + 1

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE levels
            SET xp = $1, level = $2
            WHERE user_id = $3
            """,
            new_xp,
            new_level,
            user_id
        )

    return new_xp, new_level, new_level > level

# ====================== NUMBER FORMATTER & PARSER ======================
def draw_card():
    cards = [2,3,4,5,6,7,8,9,10,10,10,10,11]  # J,Q,K = 10, Ace = 11
    return random.choice(cards)

def calculate_hand(hand):
    total = sum(hand)
    aces = hand.count(11)

    while total > 21 and aces:
        total -= 10
        aces -= 1

    return total

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

class BlackjackView(discord.ui.View):
    def __init__(self, ctx, bet, player_hand, dealer_hand):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.bet = bet
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.game_over = False

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.ctx.author.id

    def get_embed(self, reveal_dealer=False):
        player_total = calculate_hand(self.player_hand)

        dealer_display = self.dealer_hand.copy()
        if not reveal_dealer:
            dealer_display = [self.dealer_hand[0], "❓"]

        embed = discord.Embed(title="🃏 Blackjack", color=0x00ff88)
        embed.add_field(name="Your Hand", value=f"{self.player_hand} (**{player_total}**)", inline=False)
        embed.add_field(name="Dealer Hand", value=f"{dealer_display}", inline=False)
        embed.add_field(name="Bet", value=f"{self.bet:,} 🛡️ Sigils", inline=False)
        return embed

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.green)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):

        self.player_hand.append(draw_card())
        total = calculate_hand(self.player_hand)

        if total > 21:
            self.game_over = True
            await update_sigils(self.ctx.author.id, -self.bet)

            await interaction.response.edit_message(
                embed=self.get_embed(reveal_dealer=True).add_field(
                    name="💀 Result",
                    value="You busted! You lost your bet.",
                    inline=False
                ),
                view=None
            )
            self.stop()
            return

        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.red)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):

        # dealer plays
        while calculate_hand(self.dealer_hand) < 17:
            self.dealer_hand.append(draw_card())

        player_total = calculate_hand(self.player_hand)
        dealer_total = calculate_hand(self.dealer_hand)

        if dealer_total > 21 or player_total > dealer_total:
            await update_sigils(self.ctx.author.id, self.bet)
            result = "🎉 You win!"
        elif player_total < dealer_total:
            await update_sigils(self.ctx.author.id, -self.bet)
            result = "💀 You lost!"
        else:
            result = "🤝 It's a tie!"

        embed = self.get_embed(reveal_dealer=True)
        embed.add_field(name="Result", value=result, inline=False)

        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


# ====================== ERROR HANDLER ======================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"❌ Something went wrong: {error}")
    print(f"Error in command {ctx.command}: {error}")

# ====================== LEVELING SYSTEM ======================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Wait for database to be ready before processing XP
    if not db_ready:
        await bot.process_commands(message)
        return

    if message.channel.name.lower() == "commands":
        await bot.process_commands(message)
        return

    now = datetime.now(timezone.utc)
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

    try:
        new_xp, new_level, leveled_up = await add_xp(message.author.id, xp_gain)

        if leveled_up:
            level_up_channel = discord.utils.get(message.guild.text_channels, name="level-up")
            if level_up_channel:
                embed = discord.Embed(
                    title="🎉 Level Up!",
                    description=f"{message.author.mention} has reached **Level {new_level}**!",
                    color=0x00ff88
                )
                embed.add_field(name="Total XP", value=f"{new_xp:,}", inline=True)
                await level_up_channel.send(embed=embed)
    except Exception as e:
        print(f"Error adding XP: {e}")

    await bot.process_commands(message)

@bot.event
async def on_ready():
    global db_ready
    
    print(f"✅ Bot online as {bot.user}")
    
    # Initialize database in background
    if db_pool is None:
        bot.loop.create_task(init_db())

# ====================== HELPER ======================
def is_commands_channel(ctx):
    return ctx.channel.name.lower() == "commands"

# ====================== COMMANDS ======================
@bot.command(name='help')
async def help_command(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return
    
    embed = discord.Embed(title="🤖 Viltrumite Bot", description="Power, Token, Leveling system & Sigils", color=0x00ff88)
    embed.add_field(name="📋 Available Commands", value="`.pcalculate` - Power time calculator\n"
      "`.tcalculate` - Token time calculator\n"
      "`.rank` - Show your current level & progress\n"
      "`.leaderboard` - Top 10 users on the server\n"
      "`.sigils` - Check your Iron Sigils balance\n"
      "`.daily` - Claim your daily Iron Sigils\n"
      "`.sigilsinfo` - How sigils work from donations\n"
      "`.milestones` - All donation milestone rewards\n"
      "`.gamble` - Gamble your sigils for a chance to win more!\n"
      "`.checksigils` - Check your sigils balance",
      inline=False)
    embed.set_footer(text="Leveling works by chatting | Level-ups appear in #level-up")
    await ctx.send(embed=embed)

# ====================== SIGILS INFO COMMAND ======================
@bot.command(name='sigilsinfo')
async def sigilsinfo(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return

    embed = discord.Embed(
        title="🛡️ Sigils Information",
        description="**How to earn Sigils from Token Donations**",
        color=0x00ff88
    )
    embed.add_field(
        name="💰 Main Rule",
        value="For every **1,000,000 (1M)** donated tokens you receive **100 Sigils**.",
        inline=False
    )
    embed.add_field(
        name="🏆 Milestones",
        value="You also get **bonus sigils** when you hit these contribution milestones:\n"
              "• `100k` • `350k` • `500k` • `700k` • `1M` • `1.5M`\n\n"
              "Type `.milestones` to see exactly how many sigils each milestone gives!",
        inline=False
    )
    embed.add_field(
        name="🔄 Redemption",
        value="Once you reach **50,000 (50k) Sigils**, you can redeem **1 week of Titan or Deluxe Gamepass**.",
        inline=False
    )
    embed.set_footer(text="Use .milestones for full list | Donations are tracked by staff")
    
    await ctx.send(embed=embed)


# ====================== MILESTONES COMMAND ======================
@bot.command(name='milestones')
async def milestones(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return

    embed = discord.Embed(
        title="🏆 Token Contribution Milestones",
        description="Every time you hit one of these totals in contribution towards the clan.",
        color=0x00ff88
    )
    embed.add_field(name="100K",  value="**+40 Sigils**",  inline=True)
    embed.add_field(name="350K", value="**+125 Sigils**", inline=True)
    embed.add_field(name="500K", value="**+175 Sigils**", inline=True)
    embed.add_field(name="700K", value="**+250 Sigils**", inline=True)
    embed.add_field(name="1M",   value="**+300 Sigils**", inline=True)
    embed.add_field(name="1.5M", value="**+400 Sigils**", inline=True)
    
    embed.add_field(
        name="💡 Note",
        value="Milestones are weekly bonuses.",
        inline=False
    )
    embed.set_footer(text="Send a screenshot of your contributions at every milestone reached.")
    
    await ctx.send(embed=embed)


@bot.command()
async def sigils(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return
    
    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return
    
    balance = await get_sigils(ctx.author.id)
    await ctx.send(embed=discord.Embed(title="🛡️ Iron Sigils", description=f"You own **{balance:,} 🛡️ Sigils**", color=0x00ff88))

async def get_sigils(user_id: int):
    _, _, sigils = await get_user_level(user_id)
    return sigils

async def update_sigils(user_id: int, amount: int):
    xp, level, sigils = await get_user_level(user_id)
    new_balance = sigils + amount

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE levels SET sigils = $1 WHERE user_id = $2",
            new_balance,
            user_id
        )

    return new_balance

@bot.command()
@commands.has_permissions(administrator=True)
async def give(ctx, member: discord.Member, amount: int):
    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return
    
    if amount <= 0:
        return await ctx.send("❌ Amount must be positive!")
    new_balance = await update_sigils(member.id, amount)
    embed = discord.Embed(title="🛡️ Sigils Given", description=f"{ctx.author.mention} gave {member.mention} **{amount:,} 🛡️ Sigils**", color=0xffd700)
    embed.add_field(name="New Balance", value=f"{new_balance:,} 🛡️ Sigils")
    await ctx.send(embed=embed)

@give.error
async def give_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Administrator permission.")

@bot.command()
async def daily(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return

    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return

    await ensure_db()

    now = datetime.now(timezone.utc)

    async with db_pool.acquire() as conn:

        # make sure user exists
        await conn.execute("""
            INSERT INTO levels (user_id, xp, level, sigils, last_daily)
            VALUES ($1, 0, 1, 0, NULL)
            ON CONFLICT (user_id) DO NOTHING
        """, ctx.author.id)

        row = await conn.fetchrow(
            "SELECT last_daily FROM levels WHERE user_id = $1",
            ctx.author.id
        )

        if row and row["last_daily"]:
            last = datetime.fromisoformat(row["last_daily"])

            if now - last < timedelta(hours=24):
                remaining = timedelta(hours=24) - (now - last)
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds // 60) % 60
                return await ctx.send(
                    f"⏳ Daily already claimed. Try again in {hours}h {minutes}m"
                )

        reward = random.randint(100, 500)

        await conn.execute(
            "UPDATE levels SET sigils = sigils + $1, last_daily = $2 WHERE user_id = $3",
            reward,
            now.isoformat(),
            ctx.author.id
        )

    await ctx.send(f"🎁 You received **{reward} 🛡️ Iron Sigils**!")

@bot.command(name='gamble')
async def gamble(ctx, amount: str):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return

    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return

    try:
        bet = int(parse_game_number(amount))
    except:
        return await ctx.send("❌ Invalid bet amount!")

    if bet <= 0:
        return await ctx.send("❌ Bet must be greater than 0!")

    balance = await get_sigils(ctx.author.id)

    if bet > balance:
        return await ctx.send(f"❌ You only have **{balance:,} 🛡️ Sigils**!")

    win_chance = 0.42  # 42% chance to win
    multiplier = 2     # double your bet if you win

    roll = random.random()

    if roll < win_chance:
        winnings = int(bet * multiplier)
        await update_sigils(ctx.author.id, winnings - bet)

        embed = discord.Embed(
            title="🎉 You Won!",
            description=f"You bet **{bet:,}** and won **{winnings:,} 🛡️ Sigils**!",
            color=0x00ff88
        )
    else:
        await update_sigils(ctx.author.id, -bet)

        embed = discord.Embed(
            title="💀 You Lost!",
            description=f"You lost **{bet:,} 🛡️ Sigils**... better luck next time!",
            color=0xff4444
        )

    new_balance = await get_sigils(ctx.author.id)
    embed.add_field(name="New Balance", value=f"{new_balance:,} 🛡️ Sigils")

    await ctx.send(embed=embed)

@bot.command(name='checksigils')
async def checksigils(ctx, member: discord.Member = None):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return

    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return

    # If no user mentioned, default to yourself
    target = member or ctx.author

    balance = await get_sigils(target.id)

    embed = discord.Embed(
        title="🛡️ Sigils Balance",
        description=f"{target.mention} owns **{balance:,} 🛡️ Sigils**",
        color=0x00ff88
    )

    embed.set_thumbnail(url=target.display_avatar.url)

    await ctx.send(embed=embed)

@bot.command(name='xpgive')
@commands.has_permissions(administrator=True)
async def xpgive(ctx, member: discord.Member, amount: int):
    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return
    
    if amount <= 0:
        return await ctx.send("❌ Amount must be greater than 0!")

    # Add XP using your existing system
    new_xp, new_level, leveled_up = await add_xp(member.id, amount)

    embed = discord.Embed(
        title="⚡ XP Given",
        description=f"{ctx.author.mention} gave {member.mention} **{amount:,} XP**",
        color=0x00ff88
    )

    embed.add_field(name="New XP", value=f"{new_xp:,}", inline=True)
    embed.add_field(name="Level", value=f"{new_level}", inline=True)

    if leveled_up:
        embed.add_field(name="🎉 Level Up!", value=f"{member.mention} reached **Level {new_level}**!", inline=False)

    await ctx.send(embed=embed)

@xpgive.error
async def xpgive_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Administrator permission.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Usage: `.xpgive @user <amount>`")

@bot.command(name='rank')
async def rank(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return
    
    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return
    
    xp, level, _ = await get_user_level(ctx.author.id)
    current_level_xp = ((level - 1) ** 2) * 100
    next_level_xp = (level ** 2) * 100
    progress = ((xp - current_level_xp) / (next_level_xp - current_level_xp) * 100) if next_level_xp > current_level_xp else 100

    embed = discord.Embed(title=f"{ctx.author.display_name}'s Rank", color=0x00ff88)
    embed.add_field(name="Level", value=f"**{level}**", inline=True)
    embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
    embed.add_field(name="Progress", value=f"{progress:.1f}%", inline=True)
    embed.add_field(name="Progress Bar", value=progress_bar(xp - current_level_xp, next_level_xp - current_level_xp), inline=False)
    embed.add_field(name="XP to next level", value=f"{next_level_xp - xp:,}", inline=False)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name='slots')
async def slots(ctx, amount: str):
    if not is_commands_channel(ctx):
        return await ctx.send("❌ Use this in #commands!")

    try:
        bet = int(parse_game_number(amount))
    except:
        return await ctx.send("❌ Invalid bet amount!")

    if bet <= 0:
        return await ctx.send("❌ Bet must be greater than 0!")

    balance = await get_sigils(ctx.author.id)

    if bet > balance:
        return await ctx.send(f"❌ You only have {balance:,} 🛡️ Sigils!")

    emojis = ["🍒", "🍋", "🍇", "💎", "⭐", "7️⃣"]

    weights = [35, 30, 22, 8, 4, 1]  # even harsher rarity

    reel1 = random.choices(emojis, weights=weights, k=1)[0]
    reel2 = random.choices(emojis, weights=weights, k=1)[0]
    reel3 = random.choices(emojis, weights=weights, k=1)[0]

    result = f"{reel1} | {reel2} | {reel3}"

    # 💎 TRIPLE MATCH (very rare, reduced payout)
    if reel1 == reel2 == reel3:
        if reel1 == "💎":
            multiplier = 6
        elif reel1 == "7️⃣":
            multiplier = 5
        elif reel1 == "⭐":
            multiplier = 4
        else:
            multiplier = 3

        winnings = bet * multiplier
        await update_sigils(ctx.author.id, winnings - bet)

        embed = discord.Embed(
            title="🎰 BIG WIN!",
            description=f"**{result}**\nYou won **{winnings:,} 🛡️ Sigils!**",
            color=0x00ff88
        )

    # 🔁 PAIR = NO PROFIT (IMPORTANT FIX)
    elif reel1 == reel2 or reel2 == reel3 or reel1 == reel3:
        # no change to balance
        embed = discord.Embed(
            title="😐 Neutral Spin",
            description=f"**{result}**\nNo win, no loss.",
            color=0xffd700
        )

    # 💀 LOSS (most outcomes)
    else:
        await update_sigils(ctx.author.id, -bet)

        embed = discord.Embed(
            title="💀 You Lost",
            description=f"**{result}**\nYou lost **{bet:,} 🛡️ Sigils**",
            color=0xff4444
        )

    new_balance = await get_sigils(ctx.author.id)
    embed.add_field(name="New Balance", value=f"{new_balance:,} 🛡️ Sigils")

    await ctx.send(embed=embed)

@bot.command()
async def blackjack(ctx, amount: str):

    if not is_commands_channel(ctx):
        return await ctx.send("❌ Use this in #commands only!")

    try:
        bet = int(parse_game_number(amount))
    except:
        return await ctx.send("❌ Invalid bet amount!")

    if bet <= 0:
        return await ctx.send("❌ Bet must be greater than 0!")

    balance = await get_sigils(ctx.author.id)

    if bet > balance:
        return await ctx.send(f"❌ You only have {balance:,} sigils!")

    # initial hands
    player_hand = [draw_card(), draw_card()]
    dealer_hand = [draw_card(), draw_card()]

    view = BlackjackView(ctx, bet, player_hand, dealer_hand)

    await ctx.send(embed=view.get_embed(), view=view)


@bot.command(name='leaderboard')
async def leaderboard(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return
    
    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, xp, level FROM levels ORDER BY xp DESC LIMIT 10")

    if not rows:
        return await ctx.send("No users have XP yet!")

    embed = discord.Embed(title="🏆 Server Level Leaderboard", color=0xFFD700)
    desc = "\n".join(f"**#{i}** {ctx.guild.get_member(row['user_id']).display_name if ctx.guild.get_member(row['user_id']) else f'User {row['user_id']}'} — Level **{row['level']}** ({row['xp']:,} XP)" for i, row in enumerate(rows, 1))
    embed.description = desc
    await ctx.send(embed=embed)

@bot.command(name='sigilsleaderboard')
async def sigilsleaderboard(ctx):
    if not is_commands_channel(ctx):
        await ctx.send("❌ This command can only be used in the **#commands** channel!")
        return
    
    if not db_ready:
        await ctx.send("⏳ Database is still initializing, please wait a moment...")
        return
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, sigils FROM levels ORDER BY sigils DESC LIMIT 10")

    if not rows:
        return await ctx.send("No users have sigils yet!")

    embed = discord.Embed(title="🏆 Server Sigils Leaderboard", color=0xFFD700)
    desc = "\n".join(f"**#{i}** {ctx.guild.get_member(row['user_id']).display_name if ctx.guild.get_member(row['user_id']) else f'User {row['user_id']}'} — Sigils: **{row['sigils']:,}**" for i, row in enumerate(rows, 1))
    embed.description = desc
    await ctx.send(embed=embed)

# ====================== CALCULATORS ======================
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
                   "**1.** What is your **current token count**?\n"
                   "Example: `50000`, `25k`, `2.5M`, or just a number")
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel
    
    try:
        msg = await bot.wait_for('message', check=check, timeout=180)
        current_tokens = parse_game_number(msg.content)

        await ctx.send("**2.** How many **tokens do you earn per tick**?\n"
                       "Example: `150`, `2500`, `27162`")
        
        msg = await bot.wait_for('message', check=check, timeout=180)
        tokens_per_tick = parse_game_number(msg.content)

        await ctx.send("**3.** What is your **tick rate** (speed)?\n"
                       "Example: `32s`, `60`, `35.5s`")

        msg = await bot.wait_for('message', check=check, timeout=180)
        tick_rate = float(msg.content.strip().lower().replace("s", "").replace(" ", ""))

        await ctx.send("**4.** How many **tokens do you need** (goal)?\n"
                       "Example: `50000`, `605K`, `32.5M`")

        msg = await bot.wait_for('message', check=check, timeout=180)
        goal = parse_game_number(msg.content)

        if goal <= current_tokens:
            await ctx.send("🎉 You have already reached or passed your goal!")
            return

        if tokens_per_tick <= 0 or tick_rate <= 0:
            await ctx.send("❌ Tokens per tick and tick rate must be greater than 0!")
            return

        needed = goal - current_tokens
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
        embed.add_field(name="Current Tokens", value=format_game_number(current_tokens), inline=True)
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