import os
import sys
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
import traceback
from collections import defaultdict, deque
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --------------------------------------------------------
# ENHANCED LOGGING CONFIGURATION
# --------------------------------------------------------

# Setup detailed logging for better troubleshooting
logging.basicConfig(
    level=logging.INFO,  # Changed to INFO from DEBUG for less verbose logs
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ])
logger = logging.getLogger("discord_bot")

# Suppress the PyNaCl warning since we don't need voice support
discord.VoiceClient.warn_nacl = False

# --------------------------------------------------------
# COOLDOWN CONFIGURATION
# --------------------------------------------------------

cooldown_seconds = 3  # Slowmode duration in seconds
monitoring_window = 10  # Time window in seconds to count messages
activity_threshold = 5  # Messages needed to trigger cooldown
inactivity_threshold = 2  # Messages needed to disable cooldown

# --------------------------------------------------------
# BOT SETUP
# --------------------------------------------------------

# Create intents - only enable what's actually needed
intents = discord.Intents.default()
intents.message_content = True  # Required to read message content

# --------------------------------------------------------
# BOT CLASS DEFINITION
# --------------------------------------------------------


class CooldownBot(commands.Bot):

    def __init__(self):
        # Initialize bot with required intents
        super().__init__(
            command_prefix="!",
            intents=intents,
            # Add this to make sure application commands work properly
            application_id=os.getenv("APPLICATION_ID")  # Get from .env file
        )
        self.monitored_channels = set()
        self.message_history = defaultdict(lambda: deque(maxlen=100))
        self._command_sync_flags = {}  # Track command sync status
        
        # Track whether commands have already been set up
        self._commands_registered = False

    async def setup_hook(self):
        """Called when the bot is starting up"""
        try:
            # Start activity monitor task
            self.activity_monitor.start()
            logger.info("Activity monitor task started successfully")

            # Register commands only once
            if not self._commands_registered:
                self._setup_commands()
                self._commands_registered = True

            # Explicitly log all registered commands
            all_commands = [cmd.name for cmd in self.tree.get_commands()]
            logger.info(f"Registered commands at setup_hook: {all_commands}")

            # First sync for global commands (takes up to an hour to propagate)
            # Only sync commands once during setup - we'll avoid syncing again in on_ready
            try:
                logger.info("Attempting to sync commands globally...")
                await self.tree.sync()
                self._command_sync_flags['global_synced'] = True
                logger.info("Commands synced globally")
            except Exception as e:
                logger.error(f"Failed to sync commands globally: {e}")
                logger.error(traceback.format_exc())

            # For testing, also sync to the test guild if specified
            test_guild_id = os.getenv("TEST_GUILD_ID")
            if test_guild_id:
                try:
                    logger.info(
                        f"Syncing commands to test guild: {test_guild_id}")
                    test_guild = discord.Object(id=int(test_guild_id))
                    # We no longer need to copy_global_to as we've already synced globally
                    # self.tree.copy_global_to(guild=test_guild)  # Removed duplicate command registration
                    await self.tree.sync(guild=test_guild)
                    self._command_sync_flags[
                        f'guild_{test_guild_id}_synced'] = True
                    logger.info(
                        f"Commands synced to test guild {test_guild_id}")
                except Exception as e:
                    logger.error(f"Failed to sync commands to test guild: {e}")
                    logger.error(traceback.format_exc())

        except Exception as e:
            logger.error(f"Error in setup_hook: {e}")
            logger.error(traceback.format_exc())

    def _setup_commands(self):
        """Register commands during initialization"""
        logger.info("Setting up commands...")

        # You can also register commands directly
        @self.tree.command(
            name="cooldown",
            description=
            "Monitor a channel for activity and apply automatic cooldown.")
        @app_commands.describe(
            channel="The channel to monitor",
            action=
            "Choose an action: start monitoring, stop monitoring, or list monitored channels"
        )
        @app_commands.choices(action=[
            app_commands.Choice(name="start", value="start"),
            app_commands.Choice(name="stop", value="stop"),
            app_commands.Choice(name="list", value="list")
        ])
        async def cooldown_command(interaction: discord.Interaction,
                                   channel: discord.TextChannel = None,
                                   action: str = "list"):
            await self._handle_cooldown_command(interaction, channel, action)

        @self.tree.command(
            name="config",
            description="View or change cooldown bot configuration settings.")
        @app_commands.describe(setting="The setting to modify",
                               value="The new value for the setting")
        @app_commands.choices(setting=[
            app_commands.Choice(name="cooldown_duration",
                                value="cooldown_seconds"),
            app_commands.Choice(name="activity_threshold",
                                value="activity_threshold"),
            app_commands.Choice(name="inactivity_threshold",
                                value="inactivity_threshold"),
            app_commands.Choice(name="monitoring_window",
                                value="monitoring_window")
        ])
        async def config_command(interaction: discord.Interaction,
                                 setting: str = None,
                                 value: int = None):
            await self._handle_config_command(interaction, setting, value)

        logger.info(
            f"Command setup complete - registered commands: {[cmd.name for cmd in self.tree.get_commands()]}"
        )

    async def on_ready(self):
        """Called when the bot is ready"""
        logger.info(f"Bot is online as {self.user}")
        logger.info(f"Bot ID: {self.user.id}")

        # Double-check command registration
        all_commands = [cmd.name for cmd in self.tree.get_commands()]
        logger.info(f"Registered commands at on_ready: {all_commands}")

        # Log information about the guilds the bot is in
        logger.info(f"Connected to {len(self.guilds)} guilds")
        for guild in self.guilds:
            logger.info(f"Connected to guild: {guild.name} (ID: {guild.id})")

            # Check and log the bot's permissions in the guild
            bot_member = guild.get_member(self.user.id)
            if bot_member:
                permissions = bot_member.guild_permissions
                logger.info(
                    f"Bot permissions in {guild.name}: Administrator={permissions.administrator}, "
                    f"ManageChannels={permissions.manage_channels}")

                # Only sync commands if we haven't synced to this guild before
                # AND we don't have global commands synced yet
                if not self._command_sync_flags.get('global_synced', False) and \
                   not self._command_sync_flags.get(f'guild_{guild.id}_synced', False):
                    try:
                        guild_obj = discord.Object(id=guild.id)
                        # We don't need to copy_global_to as it duplicates commands
                        # self.tree.copy_global_to(guild=guild_obj)  # Removed duplicate command registration
                        await self.tree.sync(guild=guild_obj)
                        self._command_sync_flags[
                            f'guild_{guild.id}_synced'] = True
                        logger.info(f"Commands synced to guild {guild.name}")
                    except discord.HTTPException as e:
                        # Rate limit handling
                        if e.status == 429:
                            retry_after = e.retry_after
                            logger.warning(
                                f"Rate limited when syncing to guild {guild.id}. Retry after {retry_after}s"
                            )
                        else:
                            logger.error(
                                f"HTTP Exception when syncing to guild {guild.id}: {e}"
                            )

    async def on_guild_join(self, guild):
        """Called when the bot joins a new guild"""
        logger.info(f"Joined new guild: {guild.name} (ID: {guild.id})")

        # Check the bot's permissions in the new guild
        bot_member = guild.get_member(self.user.id)
        if bot_member:
            permissions = bot_member.guild_permissions
            logger.info(
                f"Bot permissions in {guild.name}: Administrator={permissions.administrator}, "
                f"ManageChannels={permissions.manage_channels}")

        # Only sync if we haven't already synced global commands
        if not self._command_sync_flags.get('global_synced', False):
            try:
                guild_obj = discord.Object(id=guild.id)
                # We don't need to copy_global_to as it duplicates commands
                # self.tree.copy_global_to(guild=guild_obj)  # Removed duplicate command registration
                await self.tree.sync(guild=guild_obj)
                self._command_sync_flags[f'guild_{guild.id}_synced'] = True
                logger.info(f"Commands synced to new guild {guild.name}")
            except discord.HTTPException as e:
                # Handle rate limits properly
                if e.status == 429:
                    retry_after = e.retry_after
                    logger.warning(
                        f"Rate limited when syncing to new guild {guild.id}. Retry after {retry_after}s"
                    )
                else:
                    logger.error(f"Failed to sync commands to new guild: {e}")

    async def on_connect(self):
        logger.info(f"Bot connected to Discord")

    async def on_disconnect(self):
        logger.warning(f"Bot disconnected from Discord")

    async def on_error(self, event, *args, **kwargs):
        """Global error handler for bot events"""
        logger.error(f"Error in event {event}")
        logger.error(traceback.format_exc())

    @tasks.loop(seconds=5)
    async def activity_monitor(self):
        """Monitor message activity and adjust slowmode accordingly"""
        now = datetime.now()
        cutoff_time = now - timedelta(seconds=monitoring_window)

        # Make a copy of the channels to avoid modification during iteration
        channels_to_check = set(self.monitored_channels)

        if not channels_to_check:
            return  # Skip if no channels are being monitored

        for channel_id in channels_to_check:
            try:
                # Get the channel - handle possible errors or None
                channel = self.get_channel(channel_id)
                if not channel:
                    logger.warning(
                        f"Channel {channel_id} not found, removing from monitored list."
                    )
                    self.monitored_channels.discard(channel_id)
                    continue

                # Safe type checking
                if not isinstance(channel, discord.TextChannel):
                    logger.warning(
                        f"Channel {channel_id} is not a text channel, removing from monitored list."
                    )
                    self.monitored_channels.discard(channel_id)
                    continue

                # Count recent messages
                recent_messages = sum(
                    1 for timestamp in self.message_history[channel_id]
                    if timestamp > cutoff_time)

                # Log the message count for debugging
                logger.info(
                    f"Channel {channel.name} ({channel_id}): {recent_messages} messages in last {monitoring_window}s"
                )

                # Safely check permissions before trying to edit
                bot_permissions = channel.permissions_for(channel.guild.me)
                if not bot_permissions.manage_channels:
                    logger.warning(
                        f"Missing 'Manage Channels' permission for {channel.name}"
                    )
                    await self._notify_permission_error(channel_id)
                    continue

                # Safely get current slowmode setting
                try:
                    current_slowmode = channel.slowmode_delay
                except AttributeError:
                    logger.error(
                        f"Channel {channel.name} doesn't support slowmode")
                    self.monitored_channels.discard(channel_id)
                    continue

                logger.info(
                    f"Channel {channel.name}: {recent_messages} messages in last {monitoring_window}s, current slowmode: {current_slowmode}s"
                )

                # Apply or remove slowmode based on activity
                if recent_messages >= activity_threshold and current_slowmode == 0:
                    logger.info(
                        f"Enabling slowmode ({cooldown_seconds}s) in #{channel.name}"
                    )
                    await channel.edit(slowmode_delay=cooldown_seconds)
                    # Notify the channel
                    try:
                        await channel.send(
                            f"🐢 Slowmode enabled due to high activity. Cooldown set to {cooldown_seconds} seconds."
                        )
                    except:
                        logger.warning(
                            f"Could not send notification to {channel.name}")

                elif recent_messages <= inactivity_threshold and current_slowmode > 0:
                    logger.info(f"Disabling slowmode in #{channel.name}")
                    await channel.edit(slowmode_delay=0)
                    # Notify the channel
                    try:
                        await channel.send(
                            "🚀 Activity has slowed down. Slowmode has been disabled."
                        )
                    except:
                        logger.warning(
                            f"Could not send notification to {channel.name}")

            except discord.Forbidden as e:
                logger.error(
                    f"Forbidden: Insufficient permissions for channel ID {channel_id}: {e}"
                )
                await self._notify_permission_error(channel_id)
                # Remove channel to prevent repeated errors
                self.monitored_channels.discard(channel_id)
            except discord.HTTPException as e:
                logger.error(f"HTTP error for channel ID {channel_id}: {e}")
            except Exception as e:
                logger.error(
                    f"Unexpected error in activity monitor for channel ID {channel_id}: {e}"
                )
                logger.error(traceback.format_exc())

    async def _notify_permission_error(self, channel_id):
        """Attempt to notify about permission errors"""
        try:
            channel = self.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                # Check if we can at least send messages
                bot_permissions = channel.permissions_for(channel.guild.me)
                if bot_permissions.send_messages:
                    await channel.send(
                        "⚠️ I don't have permission to manage slowmode in this channel. Please give me 'Manage Channel' permissions."
                    )

                # Remove channel from monitoring to prevent repeated errors
                self.monitored_channels.discard(channel_id)
                logger.info(
                    f"Removed channel {channel.name} from monitoring due to permission issues"
                )
        except Exception as e:
            logger.error(f"Failed to send permission error notification: {e}")
            # Remove channel anyway
            self.monitored_channels.discard(channel_id)

    @activity_monitor.before_loop
    async def before_activity_monitor(self):
        """Wait until bot is ready before starting tasks"""
        await self.wait_until_ready()
        logger.info("Activity monitor ready to start")

    @activity_monitor.error
    async def activity_monitor_error(self, error):
        """Handle errors in the activity monitor task"""
        logger.error(f"Error in activity monitor task: {error}")
        logger.error(traceback.format_exc())

    async def on_message(self, message):
        """Process incoming messages"""
        # Skip processing bot messages
        if message.author.bot:
            return

        # Process commands (this is for prefix commands)
        await self.process_commands(message)

        # Record message timestamp for monitored channels
        if message.channel.id in self.monitored_channels:
            logger.debug(
                f"Recorded message in channel {message.channel.id} from {message.author}"
            )
            self.message_history[message.channel.id].append(datetime.now())

    # Slash command handlers
    async def _handle_cooldown_command(self,
                                       interaction,
                                       channel,
                                       action="list"):
        """Handler for the /cooldown command"""
        try:
            # Check user permissions
            if not self._has_permission(interaction.user, interaction.guild):
                await interaction.response.send_message(
                    "You don't have permission to use this command. You need Administrator or Manage Channels permission.",
                    ephemeral=True)
                logger.info(
                    f"User {interaction.user} attempted to use cooldown command without permission"
                )
                return

            # Handle list action
            if action == "list":
                if not self.monitored_channels:
                    await interaction.response.send_message(
                        "No channels are currently being monitored.",
                        ephemeral=True)
                    return

                # Get mentions for valid channels
                channel_mentions = []
                for cid in list(
                        self.monitored_channels
                ):  # Use list to avoid modification during iteration
                    channel = self.get_channel(cid)
                    if channel:
                        channel_mentions.append(f"• {channel.mention}")
                    else:
                        # Remove invalid channel IDs
                        self.monitored_channels.discard(cid)

                if not channel_mentions:
                    await interaction.response.send_message(
                        "No valid channels are currently being monitored.",
                        ephemeral=True)
                else:
                    await interaction.response.send_message(
                        f"**Currently monitoring {len(channel_mentions)} channels:**\n"
                        + "\n".join(channel_mentions),
                        ephemeral=True)
                return

            # For other actions, channel parameter is required
            if not channel:
                await interaction.response.send_message(
                    "Please specify a channel for this action.",
                    ephemeral=True)
                return

            # Check if the bot has permissions for the channel
            bot_member = interaction.guild.me
            channel_perms = channel.permissions_for(bot_member)
            if not channel_perms.manage_channels:
                await interaction.response.send_message(
                    f"I don't have 'Manage Channels' permission for {channel.mention}. Please update my permissions.",
                    ephemeral=True)
                return

            # Handle start action
            if action == "start":
                self.monitored_channels.add(channel.id)
                logger.info(
                    f"User {interaction.user} started monitoring channel #{channel.name} ({channel.id})"
                )
                await interaction.response.send_message(
                    f"Now monitoring {channel.mention}. Cooldown will be applied when messages exceed {activity_threshold} within {monitoring_window} seconds.",
                    ephemeral=True)

                # Inform the channel that it's being monitored
                try:
                    await channel.send(
                        f"🔍 **This channel is now being monitored by CooldownBot.**\n• Slowmode will be applied when activity exceeds {activity_threshold} messages in {monitoring_window} seconds.\n• Slowmode will be disabled when activity drops below {inactivity_threshold} messages in {monitoring_window} seconds."
                    )
                except:
                    logger.warning(
                        f"Could not send notification to {channel.name}")

            # Handle stop action
            elif action == "stop":
                if channel.id in self.monitored_channels:
                    self.monitored_channels.remove(channel.id)
                    logger.info(
                        f"User {interaction.user} stopped monitoring channel #{channel.name} ({channel.id})"
                    )

                    # Reset slowmode if it was enabled
                    if channel.slowmode_delay > 0:
                        try:
                            await channel.edit(slowmode_delay=0)
                            await interaction.response.send_message(
                                f"Stopped monitoring {channel.mention} and disabled slowmode.",
                                ephemeral=True)

                            # Inform the channel
                            try:
                                await channel.send(
                                    "🛑 **Channel is no longer being monitored by CooldownBot.** Slowmode has been disabled."
                                )
                            except:
                                logger.warning(
                                    f"Could not send notification to {channel.name}"
                                )
                        except Exception as e:
                            logger.error(
                                f"Failed to disable slowmode when stopping monitor: {e}"
                            )
                            await interaction.response.send_message(
                                f"Stopped monitoring {channel.mention}, but couldn't disable slowmode.",
                                ephemeral=True)
                    else:
                        await interaction.response.send_message(
                            f"Stopped monitoring {channel.mention}.",
                            ephemeral=True)

                        # Inform the channel
                        try:
                            await channel.send(
                                "🛑 **Channel is no longer being monitored by CooldownBot.**"
                            )
                        except:
                            logger.warning(
                                f"Could not send notification to {channel.name}"
                            )
                else:
                    await interaction.response.send_message(
                        f"{channel.mention} was not being monitored.",
                        ephemeral=True)

            # Handle invalid action
            else:
                await interaction.response.send_message(
                    "Invalid action. Please use 'start', 'stop', or 'list'.",
                    ephemeral=True)

        except discord.Forbidden as e:
            logger.error(f"Permission error in cooldown command: {e}")
            await interaction.response.send_message(
                "I don't have permission to perform this action.",
                ephemeral=True)
        except Exception as e:
            logger.error(f"Error in cooldown command: {e}")
            logger.error(traceback.format_exc())
            await interaction.response.send_message(
                f"An error occurred while processing this command. Please check the bot logs.",
                ephemeral=True)

    async def _handle_config_command(self,
                                     interaction,
                                     setting=None,
                                     value=None):
        """Handler for the /config command"""
        try:
            # Check permissions
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message(
                    "You need Administrator permission to modify bot configuration.",
                    ephemeral=True)
                return

            # Global access to config variables
            global cooldown_seconds, activity_threshold, inactivity_threshold, monitoring_window

            # Current settings for display
            settings = {
                "cooldown_seconds": cooldown_seconds,
                "activity_threshold": activity_threshold,
                "inactivity_threshold": inactivity_threshold,
                "monitoring_window": monitoring_window
            }

            # Just display settings if no specific setting requested
            if not setting:
                settings_text = "\n".join(
                    [f"• **{k}**: {v}" for k, v in settings.items()])
                await interaction.response.send_message(
                    f"**Current Bot Configuration:**\n{settings_text}",
                    ephemeral=True)
                return

            # Update the specified setting if a value is provided
            if value is not None:
                if value < 0:
                    await interaction.response.send_message(
                        "Setting values must be positive numbers.",
                        ephemeral=True)
                    return

                # Update the correct global variable
                if setting == "cooldown_seconds":
                    if value > 21600:  # Discord's max slowmode is 6 hours (21600 seconds)
                        await interaction.response.send_message(
                            "Slowmode can't be longer than 6 hours (21600 seconds).",
                            ephemeral=True)
                        return
                    cooldown_seconds = value
                elif setting == "activity_threshold":
                    activity_threshold = value
                elif setting == "inactivity_threshold":
                    inactivity_threshold = value
                elif setting == "monitoring_window":
                    monitoring_window = value

                logger.info(
                    f"User {interaction.user} updated {setting} to {value}")
                await interaction.response.send_message(
                    f"Updated setting **{setting}** to **{value}**.",
                    ephemeral=True)
            else:
                # Just show the current value for the specified setting
                await interaction.response.send_message(
                    f"Current value of **{setting}** is **{settings[setting]}**.",
                    ephemeral=True)
        except Exception as e:
            logger.error(f"Error in config command: {e}")
            logger.error(traceback.format_exc())
            await interaction.response.send_message(
                "An error occurred while processing this command.",
                ephemeral=True)

    def _has_permission(self, user, guild):
        """Check if a user has permission to use the bot commands"""
        # Administrator permission always has access
        if user.guild_permissions.administrator:
            return True

        # Check if user has "Manage Channels" permission
        if user.guild_permissions.manage_channels:
            return True

        # Check role hierarchy (user must have higher role than bot)
        bot_member = guild.me
        if not bot_member:
            logger.warning(f"Could not find bot member in guild {guild.id}")
            return False

        return user.top_role > bot_member.top_role


# --------------------------------------------------------
# BOT STARTUP
# --------------------------------------------------------


def main():
    """Main function to start the bot"""
    # Get token from environment variables
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("No Discord token found in environment variables.")
        print(
            "ERROR: Discord token not found. Please set the DISCORD_TOKEN environment variable."
        )
        return

    # Check for application ID
    app_id = os.getenv("APPLICATION_ID")
    if not app_id:
        logger.warning(
            "APPLICATION_ID not found in environment variables. Slash commands may not work properly."
        )
        print(
            "WARNING: APPLICATION_ID not set. This is required for slash commands!"
        )

    # Start the bot with additional error handling
    logger.info("Starting bot...")

    # Create the bot instance
    bot = CooldownBot()

    # Run the bot with additional rate limit handling
    try:
        bot.run(token, log_handler=None)  # Disable discord.py's own logging
    except discord.LoginFailure:
        logger.error(
            "Invalid Discord token. Please check your token and try again.")
        print("ERROR: Failed to log in to Discord. Invalid token.")
    except discord.PrivilegedIntentsRequired as e:
        logger.error(
            f"Bot requires privileged intents that are not enabled in Developer Portal: {e}"
        )
        print(
            "ERROR: Privileged intents are required but not enabled in the Discord Developer Portal."
        )
        print(
            "Please enable 'Message Content Intent' in the Developer Portal.")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        logger.error(traceback.format_exc())
        print(f"ERROR: Failed to start bot: {e}")


if __name__ == "__main__":
    main()