import asyncio
import discord
import logging
import random

from webapp import webapp
from database import db
from database.helpers import ConfigurationHelper
from database.models import Configuration, Humeur, Commande
from discord import Message, TextChannel, Member, VoiceChannel, app_commands
from discordbot.humblebundle import checkHumbleBundleAndNotify
from discordbot.freeloot import checkFreeLootAndNotify
from discordbot.moderation import (
	handle_warning_command,
	handle_remove_warning_command,
	handle_list_warnings_command,
	handle_unban_command,
	handle_inspect_command,
	handle_ban_list_command,
	handle_staff_help_command,
	handle_say_command,
	handle_transfer_command,
	transfer_message_context_menu,
	moderation_slash_ban,
	moderation_slash_kick,
	moderation_slash_timeout,
	moderation_ctx_ban_author,
	moderation_ctx_kick_author,
	moderation_ctx_timeout_author,
	moderation_slash_warn,
	moderation_slash_inspect,
	moderation_ctx_warn_author,
	moderation_slash_say,
)
from discordbot.welcome import sendWelcomeMessage, sendLeaveMessage, updateInviteCache
from discordbot.rules_ack import assign_rules_arrival_on_join, register_persistent_rules_view, on_presentation_message
from discordbot.patreon import checkPatreonPosts
from discordbot.youtube import checkYouTubeVideos
from discordbot.auto_rooms import on_voice_state_update_auto_rooms, on_raw_reaction_add_auto_rooms, on_message_auto_rooms, cleanup_orphaned_auto_rooms
from discordbot.protondb_discord import protondb_slash_command, pdb_slash_command

class DiscordBot(discord.Client):
	def __init__(self, *, intents: discord.Intents):
		super().__init__(intents=intents)
		self.tree = app_commands.CommandTree(self)
		self.synced = False
	
	async def setup_hook(self):
		for cmd in (
			transfer_message_context_menu,
			moderation_slash_ban,
			moderation_slash_kick,
			moderation_slash_timeout,
			moderation_ctx_ban_author,
			moderation_ctx_kick_author,
			moderation_ctx_timeout_author,
			moderation_slash_warn,
			moderation_slash_inspect,
			moderation_ctx_warn_author,
			moderation_slash_say,
			protondb_slash_command,
			pdb_slash_command,
		):
			self.tree.add_command(cmd)
		logging.info("Commandes d'application (transfert, modération, ProtonDB) ajoutées au CommandTree")
		register_persistent_rules_view(self)
		logging.info("Vue persistante règlement (bouton) enregistrée")
	
	async def on_ready(self):
		logging.info(f'Connecté en tant que {self.user} (ID: {self.user.id})')
		webapp.config["BOT_STATUS"]["discord_connected"] = True
		webapp.config["BOT_STATUS"]["discord_guild_count"] = len(self.guilds)
		
		if not self.synced:
			try:
				logging.info("Synchronisation des commandes d'application en cours...")
				
				for guild in self.guilds:
					try:
						synced = await self.tree.sync(guild=guild)
						logging.info(f"✅ {len(synced)} commande(s) synchronisée(s) pour le serveur '{guild.name}' (ID: {guild.id})")
					except Exception as e:
						logging.error(f"❌ Erreur lors de la synchronisation pour {guild.name}: {e}")
				
				synced_global = await self.tree.sync()
				logging.info(f"✅ {len(synced_global)} commande(s) synchronisée(s) globalement")
				
				self.synced = True
				logging.info("🎉 Synchronisation complète terminée - Les commandes sont maintenant disponibles !")
			except Exception as e:
				logging.error(f"❌ Erreur lors de la synchronisation des commandes: {e}")
		
		for c in self.get_all_channels() :
			logging.info(f'{c.id} {c.name}')
		
		for guild in self.guilds:
			await updateInviteCache(guild)
		
		await cleanup_orphaned_auto_rooms(self)
		
		self.loop.create_task(self.updateStatus())
		self.loop.create_task(self.updateHumbleBundle())
		self.loop.create_task(self.updateYouTube())
		self.loop.create_task(self.updateFreeLoot())
		self.loop.create_task(self.updatePatreon())

	async def on_disconnect(self):
		webapp.config["BOT_STATUS"]["discord_connected"] = False
	
	async def updateStatus(self):
		while not self.is_closed():
			bot_status = webapp.config.get("BOT_STATUS", {})
			if bot_status.get("twitch_is_live") or bot_status.get("discord_streaming_activity"):
				await asyncio.sleep(60)
				continue
			humeurs = Humeur.query.all()
			if len(humeurs)>0 :
				humeur = random.choice(humeurs)
				if humeur != None: 
					logging.info(f'Changement de statut : {humeur.text}')
					await self.change_presence(status = discord.Status.online,  activity = discord.CustomActivity(humeur.text))
			await asyncio.sleep(10*60)

	async def updateHumbleBundle(self):
		while not self.is_closed():
			await checkHumbleBundleAndNotify(self)
			await asyncio.sleep(30*60)

	async def updateYouTube(self):
		while not self.is_closed():
			await checkYouTubeVideos()
			await asyncio.sleep(5*60)

	async def updateFreeLoot(self):
		while not self.is_closed():
			await checkFreeLootAndNotify(self)
			await asyncio.sleep(30*60)

	async def updatePatreon(self):
		while not self.is_closed():
			await checkPatreonPosts(self)
			await asyncio.sleep(10*60)

	def getAllTextChannel(self) -> list[TextChannel]:
		channels = []
		for channel in self.get_all_channels():
			if isinstance(channel, TextChannel):
				channels.append(channel)
		return channels

	def getAllVoiceChannels(self) -> list[VoiceChannel]:
		channels = []
		for channel in self.get_all_channels():
			if isinstance(channel, VoiceChannel):
				channels.append(channel)
		return channels

	def getAllRoles(self):
		guilds_roles = []
		for guild in self.guilds:
			roles = []
			for role in guild.roles:
				if role.name != "@everyone":
					roles.append(role)
			if roles:
				guilds_roles.append({
					'guild_name': guild.name,
					'guild_id': guild.id,
					'roles': roles
				})
		return guilds_roles


	def begin(self) : 
		token = Configuration.query.filter_by(key='discord_token').first()
		if token and token.value and token.value.strip():
			self.run(token.value)
		else :
			logging.error('Aucun token Discord configuré. Le bot ne peut pas être démarré')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True
bot = DiscordBot(intents=intents)

# https://discordpy.readthedocs.io/en/stable/quickstart.html
@bot.event
async def on_message(message: Message):
	if message.author == bot.user:
		return
	
	await on_presentation_message(bot, message)
	# Gestion des messages dans les auto rooms (avant le check des commandes !)
	await on_message_auto_rooms(bot, message)
	
	if not message.content.startswith('!'):
		return
	command_name = message.content.split()[0]
	
	if ConfigurationHelper().getValue('moderation_enable'):
		if command_name in ['!averto', '!av', '!avertissement', '!warn']:
			await handle_warning_command(message, bot)
			return

		if command_name in ['!delaverto', '!removewarn', '!unwarn']:
			await handle_remove_warning_command(message, bot)
			return

		if command_name in ['!listevent', '!listwarn', '!warnings']:
			await handle_list_warnings_command(message, bot)
			return
	
	if ConfigurationHelper().getValue('moderation_ban_enable'):
		if command_name == '!unban':
			await handle_unban_command(message, bot)
			return
		if command_name == '!banlist':
			await handle_ban_list_command(message, bot)
			return
	
	if ConfigurationHelper().getValue('moderation_enable'):
		if command_name == '!inspect':
			await handle_inspect_command(message, bot)
			return
	
	if command_name == '!say':
		await handle_say_command(message, bot)
		return
	
	if command_name in ['!transfert', '!transfer', '!move']:
		await handle_transfer_command(message, bot)
		return
	
	if command_name in ['!aide', '!help']:
		await handle_staff_help_command(message, bot)
		return
	
	commande = Commande.query.filter_by(discord_enable=True, trigger=command_name).first()
	if commande:
		try:
			await message.channel.send(commande.response, suppress_embeds=True)
			return
		except Exception as e:
			logging.error(f'Échec de l\'exécution de la commande Discord : {e}')

@bot.event
async def on_voice_state_update(member: Member, before, after):
	await on_voice_state_update_auto_rooms(bot, member, before, after)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
	await on_raw_reaction_add_auto_rooms(bot, payload)

@bot.event
async def on_member_join(member: Member):
	await assign_rules_arrival_on_join(bot, member)
	await sendWelcomeMessage(bot, member)

@bot.event
async def on_member_remove(member: Member):
	await sendLeaveMessage(bot, member)

@bot.event
async def on_invite_create(invite):
	await updateInviteCache(invite.guild)

@bot.event
async def on_invite_delete(invite):
	await updateInviteCache(invite.guild)

