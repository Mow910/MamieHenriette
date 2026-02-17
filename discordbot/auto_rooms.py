# discordbot/auto_rooms.py — Auto rooms : message et réactions dans la partie texte du salon vocal (onglet Discussion)
import logging
from typing import Optional

import discord
from discord import Member, VoiceState
from database.helpers import ConfigurationHelper

# (guild_id, owner_id) -> room_data (voice_channel_id, control_message_id, whitelist, blacklist, access_mode)
_rooms: dict[tuple[int, int], dict] = {}

# message_id -> (guild_id, owner_id) pour retrouver la room depuis une réaction
_control_message_ids: dict[int, tuple[int, int]] = {}

# Emoji -> action
REACTIONS = [
	("🔓", "open", "Ouvert"),
	("🔒", "closed", "Fermé"),
	("🔐", "private", "Privé"),
	("✅", "whitelist", "Liste blanche"),
	("🚫", "blacklist", "Liste noire"),
	("🧹", "purge", "Purge"),
	("👑", "transfer", "Propriété"),
	("🎤", "speak", "Micro"),
	("📹", "stream", "Vidéo"),
	("📊", "soundboards", "Soundboards"),
	("📝", "status", "Statut"),
]


def _status_display(access_mode: str) -> str:
	"""Cadenas ouvert ou fermé selon si le salon est ouvert ou pas."""
	if access_mode == "open":
		return "🔓 Ouvert"
	if access_mode == "closed":
		return "🔒 Fermé"
	if access_mode == "private":
		return "🔐 Privé"
	return "🔓 Ouvert"


def _status_emoji(access_mode: str) -> str:
	"""Emoji cadenas seul pour le nom du channel."""
	if access_mode == "private":
		return "🔐"
	return "🔓" if access_mode == "open" else "🔒"


def _build_control_embed(owner: Member, voice_channel: discord.VoiceChannel, access_mode: str, room: dict = None) -> discord.Embed:
	"""Construit l'embed de config avec infos du salon."""
	embed = discord.Embed(
		title="⚙️ Configuration du salon",
		description=(
			"Voici l'espace de configuration de votre salon vocal temporaire. "
			"Les différentes options disponibles vous permettent de personnaliser les permissions de votre salon selon vos préférences."
		),
		color=discord.Color.orange()
	)
	
	# Récupération des infos
	whitelist = room.get("whitelist", set()) if room else set()
	blacklist = room.get("blacklist", set()) if room else set()
	whitelist_text = f"{len(whitelist)} membre(s)" if whitelist else "Aucun"
	blacklist_text = f"{len(blacklist)} membre(s)" if blacklist else "Aucun"
	
	# Section Propriétaire
	embed.add_field(
		name=f"👤 Propriétaire du salon : {owner.display_name}",
		value="",
		inline=False
	)
	
	# Section Modes d'accès
	mode_open = "🔓 **Ouvert**\nLe salon sera ouvert à tous les membres, sauf ceux figurant sur la liste noire."
	mode_closed = "🔒 **Fermé**\nLe salon sera visible de tous, mais seulement accessible à la liste blanche."
	mode_private = "🔐 **Privé**\nLe salon ne sera visible et accessible qu'aux membres de la liste blanche."
	
	embed.add_field(name=mode_open, value="", inline=True)
	embed.add_field(name=mode_closed, value="", inline=True)
	embed.add_field(name=mode_private, value="", inline=True)
	
	# Section Listes
	embed.add_field(
		name="📝 **Liste blanche**",
		value=f"Les membres présents dans cette liste pourront toujours rejoindre le salon.\n\n{whitelist_text}",
		inline=True
	)
	embed.add_field(
		name="🚫 **Liste noire**",
		value=f"Les membres présents dans cette liste ne pourront jamais rejoindre le salon.\n\n{blacklist_text}",
		inline=True
	)
	embed.add_field(name="\u200b", value="", inline=True)  # Spacer
	
	# Section Purge
	embed.add_field(
		name="🧹 **Purge**",
		value="Déconnecter tous les membres du salon vocal à l'exception de ceux présents dans la liste blanche.",
		inline=False
	)
	
	# Section Transfert
	embed.add_field(
		name="👑 **Transférer**",
		value="Transférer la gestion du salon au membre de votre choix.",
		inline=False
	)
	
	# Note importante
	embed.add_field(
		name="💡",
		value="Les membres de la liste blanche ne sont pas impactés par les permissions refusées aux membres.",
		inline=False
	)
	
	embed.set_footer(text="Réagissez avec les émojis ci-dessous pour configurer votre salon")
	return embed


def _room_key(guild_id: int, owner_id: int) -> tuple[int, int]:
	return (guild_id, owner_id)


def _get_room(guild_id: int, owner_id: int) -> Optional[dict]:
	return _rooms.get(_room_key(guild_id, owner_id))


def _set_room(guild_id: int, owner_id: int, data: dict):
	_rooms[_room_key(guild_id, owner_id)] = data
	mid = data.get("control_message_id")
	if mid:
		_control_message_ids[mid] = (guild_id, owner_id)


def _del_room(guild_id: int, owner_id: int):
	data = _rooms.pop(_room_key(guild_id, owner_id), None)
	if data and data.get("control_message_id"):
		_control_message_ids.pop(data["control_message_id"], None)


def _find_room_by_channel(guild_id: int, channel_id: int) -> Optional[tuple[int, dict]]:
	for (gid, oid), data in _rooms.items():
		if gid == guild_id and data.get("voice_channel_id") == channel_id:
			return (oid, data)
	return None


def _find_room_by_message(message_id: int) -> Optional[tuple[int, int, dict]]:
	key = _control_message_ids.get(message_id)
	if not key:
		return None
	guild_id, owner_id = key
	data = _get_room(guild_id, owner_id)
	if not data:
		_control_message_ids.pop(message_id, None)
		return None
	return (guild_id, owner_id, data)


async def _apply_access_mode(channel: discord.VoiceChannel, mode: str, whitelist: set, blacklist: set):
	guild = channel.guild
	everyone = guild.default_role
	overwrites = dict(channel.overwrites)  # Récupérer les overwrites existants
	
	# Préserver les permissions existantes pour everyone (stream, speak, soundboards, etc.)
	existing_everyone_ow = overwrites.get(everyone, discord.PermissionOverwrite())
	everyone_ow = discord.PermissionOverwrite()
	
	# Copier les permissions importantes qui ne doivent pas être écrasées
	everyone_ow.stream = existing_everyone_ow.stream
	everyone_ow.speak = existing_everyone_ow.speak
	everyone_ow.use_soundboard = existing_everyone_ow.use_soundboard
	
	if mode == "open":
		everyone_ow.connect = True
		everyone_ow.view_channel = True
		# Retirer les overwrites des membres qui ne sont plus dans la blacklist
		for target in list(overwrites.keys()):
			if target != everyone and isinstance(target, discord.Member):
				if target.id not in blacklist:
					overwrites.pop(target, None)
		# Ajouter les overwrites pour la blacklist
		for uid in blacklist:
			m = guild.get_member(uid)
			if m:
				overwrites[m] = discord.PermissionOverwrite(connect=False, view_channel=True)
	elif mode == "closed":
		everyone_ow.connect = False
		everyone_ow.view_channel = True
		# Retirer les overwrites des membres qui ne sont plus dans la whitelist
		for target in list(overwrites.keys()):
			if target != everyone and isinstance(target, discord.Member):
				if target.id not in whitelist:
					overwrites.pop(target, None)
		# Ajouter les overwrites pour la whitelist
		for uid in whitelist:
			m = guild.get_member(uid)
			if m:
				overwrites[m] = discord.PermissionOverwrite(connect=True, view_channel=True)
	elif mode == "private":
		everyone_ow.connect = False
		everyone_ow.view_channel = False
		# Retirer les overwrites des membres qui ne sont plus dans la whitelist
		for target in list(overwrites.keys()):
			if target != everyone and isinstance(target, discord.Member):
				if target.id not in whitelist:
					overwrites.pop(target, None)
		# Ajouter les overwrites pour la whitelist
		for uid in whitelist:
			m = guild.get_member(uid)
			if m:
				overwrites[m] = discord.PermissionOverwrite(connect=True, view_channel=True)
	
	overwrites[everyone] = everyone_ow
	await channel.edit(overwrites=overwrites)


async def _handle_reaction_action(bot: discord.Client, guild_id: int, owner_id: int, action: str, channel):
	"""channel = salon vocal (partie texte / onglet Discussion)."""
	room = _get_room(guild_id, owner_id)
	if not room:
		await channel.send("Ce salon n'existe plus.")
		return
	voice_channel = bot.get_channel(room["voice_channel_id"])
	if not voice_channel or not isinstance(voice_channel, discord.VoiceChannel):
		await channel.send("Salon vocal introuvable.")
		return

	if action in ("open", "closed", "private"):
		room["access_mode"] = action
		await _apply_access_mode(voice_channel, action, room.get("whitelist", set()), room.get("blacklist", set()))
		# Mettre à jour le cadenas dans le nom du channel
		try:
			base_name = voice_channel.name.rstrip(" 🔓🔒🔐")
			new_name = f"{base_name} {_status_emoji(action)}"
			await voice_channel.edit(name=new_name)
		except discord.HTTPException:
			pass
		await channel.send(f"Accès du salon défini sur **{_status_display(action)}**.")
		# Mettre à jour l'embed
		await _update_control_panel(bot, guild_id, owner_id, channel)

	elif action == "whitelist":
		whitelist = room.get("whitelist", set())
		whitelist_text = ", ".join([f"<@{uid}>" for uid in whitelist]) if whitelist else "Aucun membre"
		await channel.send(
			f"**📝 Liste blanche actuelle :** {whitelist_text}\n\n"
			f"Mentionnez un membre pour l'ajouter ou le retirer de la liste blanche."
		)
		room["awaiting_whitelist"] = True

	elif action == "blacklist":
		blacklist = room.get("blacklist", set())
		blacklist_text = ", ".join([f"<@{uid}>" for uid in blacklist]) if blacklist else "Aucun membre"
		await channel.send(
			f"**🚫 Liste noire actuelle :** {blacklist_text}\n\n"
			f"Mentionnez un membre pour l'ajouter ou le retirer de la liste noire."
		)
		room["awaiting_blacklist"] = True

	elif action == "purge":
		whitelist = room.get("whitelist", set())
		kicked = 0
		for member in list(voice_channel.members):
			if member.id == owner_id or member.id in whitelist:
				continue
			try:
				await member.move_to(None)
				kicked += 1
			except discord.HTTPException:
				pass
		await channel.send(f"🧹 Purge effectuée : {kicked} membre(s) déconnecté(s).")

	elif action == "transfer":
		await channel.send(
			f"**👑 Transfert de propriété**\n\n"
			f"Mentionnez le membre à qui vous souhaitez transférer la gestion du salon."
		)
		room["awaiting_transfer"] = True

	elif action in ("speak", "stream", "soundboards"):
		everyone = voice_channel.guild.default_role
		overwrites = dict(voice_channel.overwrites)
		ow = overwrites.get(everyone) or discord.PermissionOverwrite()
		
		# BUG FIX : Par défaut, Discord autorise stream, speak et soundboards
		# Si la permission n'est pas explicitement définie (None), on considère qu'elle est True
		current = getattr(ow, action)
		if current is None:
			# Permission non définie = autorisée par défaut dans Discord
			# On veut la désactiver lors du premier clic
			setattr(ow, action, False)
			new_value = False
		else:
			# Permission définie, on l'inverse
			setattr(ow, action, not current)
			new_value = not current
		
		overwrites[everyone] = ow
		await voice_channel.edit(overwrites=overwrites)
		
		labels = {"speak": "Micro", "stream": "Vidéo/Partage d'écran", "soundboards": "Soundboards"}
		label = labels.get(action, action.capitalize())
		await channel.send(f"{label} : {'autorisé' if new_value else 'désactivé'} pour tous.")

	elif action == "status":
		current_status = voice_channel.status or "Aucun statut défini"
		await channel.send(
			f"**Statut actuel du salon :** {current_status}\n\n"
			f"Pour modifier le statut du salon (le texte affiché en haut du salon vocal), "
			f"répondez avec le nouveau statut (max 500 caractères).\n"
			f"💡 Pour supprimer le statut, répondez avec `clear` ou `effacer`."
		)
		room["awaiting_status"] = True


async def _update_control_panel(bot: discord.Client, guild_id: int, owner_id: int, channel):
	"""Met à jour le panneau de contrôle avec les nouvelles informations."""
	room = _get_room(guild_id, owner_id)
	if not room:
		return
	
	control_message_id = room.get("control_message_id")
	if not control_message_id:
		return
	
	voice_channel = bot.get_channel(room["voice_channel_id"])
	if not voice_channel or not isinstance(voice_channel, discord.VoiceChannel):
		return
	
	owner = voice_channel.guild.get_member(owner_id)
	if not owner:
		return
	
	try:
		msg = await channel.fetch_message(control_message_id)
		embed = _build_control_embed(owner, voice_channel, room.get("access_mode", "open"), room)
		await msg.edit(embed=embed)
	except discord.HTTPException:
		pass


async def send_control_panel(bot: discord.Client, guild_id: int, owner: Member, voice_channel: discord.VoiceChannel, room: dict) -> Optional[int]:
	"""Envoie le message de config avec réactions dans la partie texte du salon vocal (onglet Discussion). Seul le proprio peut réagir. Retourne l'id du message."""
	embed = _build_control_embed(owner, voice_channel, "open", room)

	try:
		# Message dans la partie texte du vocal (onglet Discussion à droite)
		msg = await voice_channel.send(embed=embed)
		for emoji, _action, _label in REACTIONS:
			await msg.add_reaction(emoji)
		return msg.id
	except discord.HTTPException as e:
		logging.error(f"Impossible d'envoyer le panneau Auto Room dans le vocal : {e}")
		return None


async def on_voice_state_update_auto_rooms(bot: discord.Client, member: Member, before: VoiceState, after: VoiceState):
	config = ConfigurationHelper()
	if not config.getValue("auto_rooms_enable"):
		return
	trigger_channel_id = config.getIntValue("auto_rooms_channel_id")
	if not trigger_channel_id:
		return

	guild = member.guild

	if after.channel and after.channel.id == trigger_channel_id:
		category = after.channel.category
		# Nom du salon avec statut (cadenas) à la création
		channel_name = f"Salon de {member.display_name} {_status_emoji('open')}"
		try:
			new_channel = await guild.create_voice_channel(
				name=channel_name,
				category=category,
				reason="Auto room"
			)
			await member.move_to(new_channel)
			
			# Créer la room data d'abord
			room_data = {
				"guild_id": guild.id,
				"voice_channel_id": new_channel.id,
				"control_message_id": None,  # Sera mis à jour après
				"owner_id": member.id,
				"whitelist": set(),
				"blacklist": set(),
				"access_mode": "open",
			}
			
			control_message_id = await send_control_panel(bot, guild.id, member, new_channel, room_data)
			room_data["control_message_id"] = control_message_id
			_set_room(guild.id, member.id, room_data)
			
			logging.info(f"Auto room créé : {new_channel.name} pour {member.display_name}")
		except discord.HTTPException as e:
			logging.error(f"Erreur création auto room : {e}")

	if before.channel and before.channel.id != trigger_channel_id:
		result = _find_room_by_channel(guild.id, before.channel.id)
		if result:
			owner_id, room = result
			remaining = [m for m in before.channel.members if m.id != member.id]
			if member.id == owner_id:
				_del_room(guild.id, owner_id)
				try:
					await before.channel.delete(reason="Propriétaire parti (auto room)")
				except discord.HTTPException:
					pass
			elif len(remaining) == 0:
				_del_room(guild.id, owner_id)
				try:
					await before.channel.delete(reason="Auto room vide")
				except discord.HTTPException:
					pass


async def on_message_auto_rooms(bot: discord.Client, message: discord.Message):
	"""Gère les messages dans les salons vocaux pour les actions (statut, liste blanche/noire, etc.)."""
	if message.author.bot:
		return
	if not ConfigurationHelper().getValue("auto_rooms_enable"):
		return
	
	# Vérifier si c'est dans un salon vocal (partie texte)
	if not isinstance(message.channel, discord.VoiceChannel):
		return
	
	# Trouver si c'est une auto room
	result = _find_room_by_channel(message.guild.id, message.channel.id)
	if not result:
		return
	
	owner_id, room = result
	
	# Seul le propriétaire peut interagir
	if message.author.id != owner_id:
		return
	
	voice_channel = message.channel
	
	# Gestion du statut de salon
	if room.get("awaiting_status"):
		room["awaiting_status"] = False
		new_status = message.content.strip()
		
		try:
			if new_status.lower() in ("clear", "effacer", "supprimer", "delete"):
				await voice_channel.edit(status=None)
				await message.channel.send("✅ Le statut du salon a été supprimé.")
			elif len(new_status) > 500:
				await message.channel.send("❌ Le statut ne peut pas dépasser 500 caractères.")
				room["awaiting_status"] = True  # Réessayer
			else:
				await voice_channel.edit(status=new_status)
				await message.channel.send(f"✅ Le statut du salon a été mis à jour : **{new_status}**")
		except discord.HTTPException as e:
			await message.channel.send(f"❌ Erreur lors de la modification du statut : {e}")
		return
	
	# Gestion de la liste blanche (si en attente)
	if room.get("awaiting_whitelist"):
		room["awaiting_whitelist"] = False
		if message.mentions:
			target = message.mentions[0]
			whitelist = room.get("whitelist", set())
			if target.id in whitelist:
				whitelist.remove(target.id)
				await message.channel.send(f"✅ {target.mention} a été retiré de la liste blanche.")
			else:
				whitelist.add(target.id)
				await message.channel.send(f"✅ {target.mention} a été ajouté à la liste blanche.")
			room["whitelist"] = whitelist
			await _apply_access_mode(voice_channel, room.get("access_mode", "open"), whitelist, room.get("blacklist", set()))
			await _update_control_panel(bot, message.guild.id, owner_id, message.channel)
		return
	
	# Gestion de la liste noire (si en attente)
	if room.get("awaiting_blacklist"):
		room["awaiting_blacklist"] = False
		if message.mentions:
			target = message.mentions[0]
			blacklist = room.get("blacklist", set())
			if target.id in blacklist:
				blacklist.remove(target.id)
				await message.channel.send(f"✅ {target.mention} a été retiré de la liste noire.")
			else:
				blacklist.add(target.id)
				await message.channel.send(f"✅ {target.mention} a été ajouté à la liste noire.")
			room["blacklist"] = blacklist
			await _apply_access_mode(voice_channel, room.get("access_mode", "open"), room.get("whitelist", set()), blacklist)
			await _update_control_panel(bot, message.guild.id, owner_id, message.channel)
		return
	
	# Gestion du transfert de propriété (si en attente)
	if room.get("awaiting_transfer"):
		room["awaiting_transfer"] = False
		if message.mentions:
			new_owner = message.mentions[0]
			if new_owner.id == owner_id:
				await message.channel.send("❌ Vous êtes déjà le propriétaire du salon.")
				return
			
			# Transférer la propriété
			old_owner_id = owner_id
			_del_room(message.guild.id, old_owner_id)
			room["owner_id"] = new_owner.id
			_set_room(message.guild.id, new_owner.id, room)
			
			# Renommer le salon
			try:
				base_name = f"Salon de {new_owner.display_name}"
				new_name = f"{base_name} {_status_emoji(room.get('access_mode', 'open'))}"
				await voice_channel.edit(name=new_name)
			except discord.HTTPException:
				pass
			
			await message.channel.send(f"✅ La propriété du salon a été transférée à {new_owner.mention}.")
			await _update_control_panel(bot, message.guild.id, new_owner.id, message.channel)
		return


async def on_raw_reaction_add_auto_rooms(bot: discord.Client, payload: discord.RawReactionActionEvent):
	"""Seul le propriétaire peut réagir ; on retire la réaction des autres."""
	if payload.user_id == bot.user.id:
		return
	if not ConfigurationHelper().getValue("auto_rooms_enable"):
		return
	room_info = _find_room_by_message(payload.message_id)
	if not room_info:
		return
	guild_id, owner_id, room = room_info
	if payload.user_id != owner_id:
		try:
			channel = bot.get_channel(payload.channel_id)
			if channel:
				msg = await channel.fetch_message(payload.message_id)
				user = payload.member or await bot.fetch_user(payload.user_id)
				await msg.remove_reaction(payload.emoji, user)
		except discord.HTTPException:
			pass
		return

	emoji_str = str(payload.emoji)
	action = None
	for e, a, _ in REACTIONS:
		if e == emoji_str:
			action = a
			break
	if not action:
		return

	# Canal = salon vocal (le message est dans la partie texte du vocal)
	channel = bot.get_channel(payload.channel_id)
	if not channel or not hasattr(channel, "send"):
		return

	await _handle_reaction_action(bot, guild_id, owner_id, action, channel)

	try:
		msg = await channel.fetch_message(payload.message_id)
		user = payload.member or await bot.fetch_user(payload.user_id)
		await msg.remove_reaction(payload.emoji, user)
	except discord.HTTPException:
		pass
