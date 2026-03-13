import discord
from discord.ext import tasks
import aiohttp
import os

# Safely pulling secrets from environment variables
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
CHANNEL_ID = int(os.environ.get('CHANNEL_ID', 0))
MINECRAFT_IP = os.environ.get('MINECRAFT_IP')

MESSAGE_ID_FILE = 'data/message_id.txt'

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Softly logged in as {client.user}')
    update_embed.start()

@tasks.loop(minutes=5)
async def update_embed():
    try:
        url = f"https://api.mcsrvstat.us/3/default/{MINECRAFT_IP}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                response = await resp.json()

        channel = client.get_channel(CHANNEL_ID)
        if not channel:
            print("Could not find the channel.")
            return

        online = response.get("online", False)

        # Crafting the pretty embed
        embed = discord.Embed(
            title="🌿 Minecraft Server Status",
            description=f"**IP:** `{MINECRAFT_IP}`",
            color=discord.Color.brand_green() if online else discord.Color.brand_red()
        )

        if online:
            players = response.get("players", {}).get("online", 0)
            max_players = response.get("players", {}).get("max", 0)
            version = response.get("version", "Unknown")
            motd_list = response.get("motd", {}).get("clean", [])
            motd = motd_list[0] if motd_list else ""

            embed.add_field(name="Status", value="🟢 Online", inline=True)
            embed.add_field(name="Players", value=f"{players}/{max_players}", inline=True)
            embed.add_field(name="Version", value=version, inline=True)

            if motd:
                embed.add_field(name="MOTD", value=f"*{motd}*", inline=False)

            # This pulls the server's icon automatically if it has one!
            embed.set_thumbnail(url=f"https://api.mcsrvstat.us/icon/{MINECRAFT_IP}")
        else:
            embed.description = "The server is currently resting."
            embed.add_field(name="Status", value="🔴 Offline", inline=True)
            embed.add_field(name="Players", value="0/0", inline=True)

        embed.set_footer(text="Updated automatically")
        embed.timestamp = discord.utils.utcnow()

        # Check if there is already a message to update
        message_id = None
        if os.path.exists(MESSAGE_ID_FILE):
            with open(MESSAGE_ID_FILE, 'r') as f:
                content = f.read().strip()
                if content.isdigit():
                    message_id = int(content)

        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed)
                print("Updated the embed.")
                return
            except discord.NotFound:
                pass  # The message was deleted, so we will just send a new one

        # Send a fresh message if one wasn't found
        msg = await channel.send(embed=embed)

        # Ensure the data directory exists before saving
        os.makedirs('data', exist_ok=True)
        with open(MESSAGE_ID_FILE, 'w') as f:
            f.write(str(msg.id))
        print("Sent a fresh embed.")

    except Exception as e:
        print(f"An error occurred: {e}")

client.run(DISCORD_TOKEN)