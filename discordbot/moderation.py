import asyncio
import logging
import time
import os
import re
import discord
import io
from typing import Optional
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from database import db
from database.helpers import ConfigurationHelper
from database.models import ModerationEvent
from discord import Message, TextChannel, ForumChannel, Thread, app_commands
from discord.ui import Modal, TextInput, View, Select, ChannelSelect

def _get_local_tz():
	tz_name = os.environ.get('APP_TZ') or os.environ.get('TZ') or 'Europe/Paris'
	try:
		return ZoneInfo(tz_name)
	except Exception:
		try:
			return datetime.now().astimezone().tzinfo or timezone.utc
		except Exception:
			return timezone.utc

def _to_local(dt: datetime) -> datetime | None:
	if not dt:
		return None
	if dt.tzinfo is None:
		# Assume stored in UTC if naive
		dt = dt.replace(tzinfo=timezone.utc)
	return dt.astimezone(_get_local_tz())

def get_staff_role_ids():
	staff_roles = ConfigurationHelper().getValue('moderation_staff_role_ids')
	if staff_roles:
		return [int(role_id.strip()) for role_id in staff_roles.split(',') if role_id.strip()]
	staff_role_old = ConfigurationHelper().getValue('moderation_staff_role_id')
	if staff_role_old:
		return [int(staff_role_old)]
	return []

def has_staff_role(user_roles):
	staff_role_ids = get_staff_role_ids()
	if not staff_role_ids:
		return False
	return any(role.id in staff_role_ids for role in user_roles)

def get_embed_delete_delay():
	delay = ConfigurationHelper().getValue('moderation_embed_delete_delay')
	return int(delay) if delay else 0

async def delete_after_delay(message):
	delay = get_embed_delete_delay()
	if delay > 0:
		await asyncio.sleep(delay)
		try:
			await message.delete()
		except:
			pass

async def safe_delete_message(message: Message):
	try:
		await message.delete()
	except:
		pass

async def send_to_moderation_log_channel(bot, embed):
	try:
		channel_id = ConfigurationHelper().getIntValue('moderation_log_channel_id')
		if not channel_id:
			logging.warning("Aucun canal de logs de modération configuré")
			return
		
		channel = bot.get_channel(channel_id)
		if not channel:
			logging.warning(f"Canal de logs de modération introuvable (ID: {channel_id})")
			return
		
		await channel.send(embed=embed)
	except Exception as e:
		logging.error(f"Erreur lors de l'envoi dans le canal de logs : {e}")

async def send_access_denied(channel):
	embed = discord.Embed(
		title="❌ Accès refusé",
		description="Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
		color=discord.Color.red()
	)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

async def send_user_not_found(channel):
	embed = discord.Embed(
		title="❌ Erreur",
		description="Utilisateur introuvable. Vérifiez la mention ou l'ID Discord.",
		color=discord.Color.red()
	)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

def parse_timeout_duration(text: str):
	match = re.search(r'--to(?:meout)?[= ]?(\d+)([smhj])?', text.lower())
	if not match:
		return None
	
	value = int(match.group(1))
	unit = match.group(2) or 'm'
	
	if unit == 's':
		return value
	elif unit == 'm':
		return value * 60
	elif unit == 'h':
		return value * 3600
	elif unit == 'j':
		return value * 86400
	return None

def format_timeout_duration(seconds: int) -> str:
	if seconds < 60:
		return f"{seconds} seconde{'s' if seconds > 1 else ''}"
	elif seconds < 3600:
		minutes = seconds // 60
		return f"{minutes} minute{'s' if minutes > 1 else ''}"
	elif seconds < 86400:
		hours = seconds // 3600
		return f"{hours} heure{'s' if hours > 1 else ''}"
	else:
		days = seconds // 86400
		return f"{days} jour{'s' if days > 1 else ''}"

async def parse_target_user_and_reason(message, bot, parts: list):
	full_text = message.content
	timeout_seconds = parse_timeout_duration(full_text)
	
	if message.mentions:
		target_user = message.mentions[0]
		reason_text = parts[2] if len(parts) > 2 else "Sans raison"
		reason_text = re.sub(r'--to(?:meout)?[= ]?\d+[smhj]?', '', reason_text, flags=re.IGNORECASE).strip()
		if not reason_text:
			reason_text = "Sans raison"
		return target_user, reason_text, timeout_seconds
	
	try:
		user_id = int(parts[1])
		target_user = await bot.fetch_user(user_id)
		reason_text = parts[2] if len(parts) > 2 else "Sans raison"
		reason_text = re.sub(r'--to(?:meout)?[= ]?\d+[smhj]?', '', reason_text, flags=re.IGNORECASE).strip()
		if not reason_text:
			reason_text = "Sans raison"
		return target_user, reason_text, timeout_seconds
	except (ValueError, discord.NotFound):
		return None, None, None

async def send_warning_usage(channel):
	embed = discord.Embed(
		title="📋 Utilisation de la commande",
		description="**Syntaxe :** `!averto @utilisateur raison` ou `!averto <id> raison`\n**Option :** Ajouter `--to durée` pour exclure temporairement l'utilisateur",
		color=discord.Color.blue()
	)
	embed.add_field(name="Exemples", value="• `!averto @User Spam dans le chat`\n• `!warn @User Comportement inapproprié --to 10m`\n• `!av @User --to 1h`\n• `!warn @User Spam --to 1j`", inline=False)
	embed.add_field(name="Durées", value="`s` = secondes, `m` = minutes (défaut), `h` = heures, `j` = jours\nExemple: `--to 10m` ou `--to 60s`", inline=False)
	embed.add_field(name="Aliases", value="`!averto`, `!av`, `!avertissement`, `!warn`", inline=False)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

def create_warning_event(target_user, reason: str, staff_member):
	event = ModerationEvent(
		type='warning',
		username=target_user.name,
		discord_id=str(target_user.id),
		created_at=datetime.now(timezone.utc),
		reason=reason,
		staff_id=str(staff_member.id),
		staff_name=staff_member.name
	)
	db.session.add(event)
	_commit_with_retry()

def _commit_with_retry(max_retries: int = 5, base_delay: float = 0.1):
	attempt = 0
	while True:
		try:
			db.session.commit()
			return
		except Exception as e:
			msg = str(e)
			if 'database is locked' in msg.lower() and attempt < max_retries:
				db.session.rollback()
				delay = base_delay * (2 ** attempt)
				time.sleep(delay)
				attempt += 1
				continue
			db.session.rollback()
			raise

async def send_dm_to_warned_user(target_user, reason: str, guild_name: str):
	try:
		dm_embed = discord.Embed(
			title="⚠️ Avertissement",
			description=f"Vous avez reçu un avertissement sur le serveur **{guild_name}**",
			color=discord.Color.orange(),
			timestamp=datetime.now(timezone.utc)
		)
		if reason != "Sans raison":
			dm_embed.add_field(name="📝 Raison", value=reason, inline=False)
		dm_embed.add_field(name="ℹ️ Information", value="Si vous avez des questions concernant cet avertissement, vous pouvez contacter l'équipe de modération.", inline=False)
		await target_user.send(embed=dm_embed)
		return True
	except discord.Forbidden:
		logging.warning(f"Impossible d'envoyer un MP à {target_user.name} ({target_user.id}) - MPs désactivés")
		return False
	except Exception as e:
		logging.error(f"Erreur lors de l'envoi du MP à {target_user.name} ({target_user.id}): {e}")
		return False

async def send_warning_confirmation(channel, target_user, reason: str, original_message: Message, bot, timeout_info: tuple = None):
	local_now = _to_local(datetime.now(timezone.utc))
	dm_sent = await send_dm_to_warned_user(target_user, reason, original_message.guild.name)
	
	was_timed_out = timeout_info is not None and timeout_info[0]
	timeout_duration = timeout_info[1] if timeout_info else None
	
	title = "⚠️ Avertissement + ⏱️ Time out" if was_timed_out else "⚠️ Avertissement"
	description = f"**{target_user.name}** (`{target_user.name}`) a reçu un avertissement"
	if was_timed_out:
		description += f" et a été time out ({format_timeout_duration(timeout_duration)})"
	
	embed = discord.Embed(
		title=title,
		description=description,
		color=discord.Color.orange(),
		timestamp=datetime.now(timezone.utc)
	)
	embed.add_field(name="👤 Utilisateur", value=f"**{target_user.name}**\n`{target_user.id}`", inline=True)
	embed.add_field(name="🛡️ Modérateur", value=f"**{original_message.author.name}**", inline=True)
	embed.add_field(name="📅 Date et heure", value=local_now.strftime('%d/%m/%Y à %H:%M'), inline=True)
	if reason != "Sans raison":
		embed.add_field(name="📝 Raison", value=reason, inline=False)
	
	if dm_sent:
		embed.add_field(name="✅ Message privé", value="L'utilisateur a été notifié par MP", inline=False)
	else:
		embed.add_field(name="⚠️ Message privé", value=f"Il faut contacter {target_user.mention} pour l'informer de cet avertissement (MPs désactivés). {original_message.author.mention}", inline=False)
	
	embed.set_footer(text=f"ID: {target_user.id} • Serveur: {original_message.guild.name}")
	
	await send_to_moderation_log_channel(bot, embed)
	await safe_delete_message(original_message)

async def handle_warning_command(message: Message, bot):
	parts = message.content.split(maxsplit=2)
	if not has_staff_role(message.author.roles):
		await send_access_denied(message.channel)
	elif len(parts) < 2:
		await send_warning_usage(message.channel)
	else:
		target_user, reason, timeout_seconds = await parse_target_user_and_reason(message, bot, parts)
		if not target_user:
			await send_user_not_found(message.channel)
		else:
			await _process_warning_success(message, target_user, reason, bot, timeout_seconds)

async def _process_warning_success(message: Message, target_user, reason: str, bot, timeout_seconds: int = None):
	create_warning_event(target_user, reason, message.author)
	
	timeout_info = None
	if timeout_seconds:
		member_obj = message.guild.get_member(target_user.id)
		if member_obj:
			try:
				until = discord.utils.utcnow() + timedelta(seconds=timeout_seconds)
				await member_obj.timeout(until, reason=reason)
				timeout_info = (True, timeout_seconds)
				
				timeout_event = ModerationEvent(
					type='timeout',
					username=target_user.name,
					discord_id=str(target_user.id),
					created_at=datetime.now(timezone.utc),
					reason=reason,
					staff_id=str(message.author.id),
					staff_name=message.author.name,
					duration=timeout_seconds
				)
				db.session.add(timeout_event)
				_commit_with_retry()
			except discord.Forbidden:
				logging.error(f"Permissions insuffisantes pour timeout {target_user.name}")
			except Exception as e:
				logging.error(f"Erreur lors du timeout de {target_user.name}: {e}")
	
	await send_warning_confirmation(message.channel, target_user, reason, message, bot, timeout_info)

def parse_timeout_from_args(duration_str: str):
	match = re.match(r'^(\d+)([smhj])?$', duration_str.lower())
	if not match:
		return None
	
	value = int(match.group(1))
	unit = match.group(2) or 'm'
	
	if unit == 's':
		return value
	elif unit == 'm':
		return value * 60
	elif unit == 'h':
		return value * 3600
	elif unit == 'j':
		return value * 86400
	return None

async def send_remove_warning_usage(channel):
	embed = discord.Embed(
		title="📋 Utilisation de la commande",
		description="**Syntaxe :** `!delaverto <id>`",
		color=discord.Color.blue()
	)
	embed.add_field(name="Exemples", value="• `!delaverto 5`\n• `!removewarn 12`", inline=False)
	embed.add_field(name="Aliases", value="`!delaverto`, `!removewarn`, `!delwarn`", inline=False)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

async def send_invalid_event_id(channel):
	embed = discord.Embed(
		title="❌ Erreur",
		description="L'ID doit être un nombre entier.",
		color=discord.Color.red()
	)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

async def send_event_not_found(channel, event_id: int):
	embed = discord.Embed(
		title="❌ Erreur",
		description=f"Aucun événement de modération trouvé avec l'ID `{event_id}`.",
		color=discord.Color.red()
	)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

def delete_moderation_event(event: ModerationEvent):
	db.session.delete(event)
	db.session.commit()

async def send_event_deleted_confirmation(channel, event: ModerationEvent, moderator, original_message: Message):
	embed = discord.Embed(
		title="✅ Événement supprimé",
		description=f"L'événement de type **{event.type}** pour **{event.username}** (ID: {event.id}) a été supprimé.",
		color=discord.Color.green(),
		timestamp=datetime.now(timezone.utc)
	)
	embed.add_field(name="🛡️ Modérateur", value=f"{moderator.name}\n`{moderator.id}`", inline=True)
	embed.set_footer(text="Mamie Henriette")
	
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))
	await safe_delete_message(original_message)

async def handle_remove_warning_command(message: Message, bot):
	if not has_staff_role(message.author.roles):
		await send_access_denied(message.channel)
		return
	
	parts = message.content.split(maxsplit=1)
	
	if len(parts) < 2:
		await send_remove_warning_usage(message.channel)
		return
	
	try:
		event_id = int(parts[1])
	except ValueError:
		await send_invalid_event_id(message.channel)
		return
	
	event = ModerationEvent.query.filter_by(id=event_id).first()
	
	if not event:
		await send_event_not_found(message.channel, event_id)
		return
	
	delete_moderation_event(event)
	await send_event_deleted_confirmation(message.channel, event, message.author, message)

def get_moderation_events(user_filter: str = None):
	if user_filter:
		return ModerationEvent.query.filter_by(discord_id=user_filter).order_by(ModerationEvent.created_at.desc()).all()
	return ModerationEvent.query.order_by(ModerationEvent.created_at.desc()).all()

async def send_no_events_found(channel):
	embed = discord.Embed(
		title="📋 Liste des événements",
		description="Aucun événement de modération trouvé.",
		color=discord.Color.blue()
	)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

def create_events_list_embed(events: list, page_num: int, per_page: int):
	start = page_num * per_page
	end = start + per_page
	page_events = events[start:end]
	max_page = (len(events) - 1) // per_page
	
	embed = discord.Embed(
		title="📋 Liste des événements de modération",
		description=f"Total : {len(events)} événement(s)",
		color=discord.Color.blue(),
		timestamp=datetime.now(timezone.utc)
	)
	
	for event in page_events:
		local_dt = _to_local(event.created_at)
		date_str = local_dt.strftime('%d/%m/%Y %H:%M') if local_dt else 'N/A'
		embed.add_field(
			name=f"ID {event.id} - {event.type.upper()} - {event.username}",
			value=f"**Discord ID:** `{event.discord_id}`\n**Date:** {date_str}\n**Raison:** {event.reason}\n**Staff:** {event.staff_name}",
			inline=False
		)
	
	embed.set_footer(text=f"Page {page_num + 1}/{max_page + 1}")
	return embed

async def add_pagination_reactions(msg, max_page: int):
	if max_page > 0:
		await msg.add_reaction('⬅️')
		await msg.add_reaction('➡️')
	await msg.add_reaction('❌')

async def handle_pagination_loop(msg, bot, message_author, events: list, per_page: int):
	page = 0
	max_page = (len(events) - 1) // per_page
	
	def check(reaction, user):
		return user == message_author and str(reaction.emoji) in ['⬅️', '➡️', '❌'] and reaction.message.id == msg.id
	
	while True:
		try:
			reaction, user = await bot.wait_for('reaction_add', timeout=60.0, check=check)
			
			if str(reaction.emoji) == '❌':
				await msg.delete()
				break
			elif str(reaction.emoji) == '➡️' and page < max_page:
				page += 1
				await msg.edit(embed=create_events_list_embed(events, page, per_page))
			elif str(reaction.emoji) == '⬅️' and page > 0:
				page -= 1
				await msg.edit(embed=create_events_list_embed(events, page, per_page))
			
			await msg.remove_reaction(reaction, user)
		except:
			break
	
	try:
		await msg.clear_reactions()
	except:
		pass

async def handle_list_warnings_command(message: Message, bot):
	if not has_staff_role(message.author.roles):
		await send_access_denied(message.channel)
		return
	
	parts = message.content.split(maxsplit=1)
	user_filter = str(message.mentions[0].id) if len(parts) > 1 and message.mentions else None
	
	events = get_moderation_events(user_filter)
	
	if not events:
		await send_no_events_found(message.channel)
		return
	
	per_page = 5
	max_page = (len(events) - 1) // per_page
	
	msg = await message.channel.send(embed=create_events_list_embed(events, 0, per_page))
	await add_pagination_reactions(msg, max_page)
	await handle_pagination_loop(msg, bot, message.author, events, per_page)
	await safe_delete_message(message)

def _create_ban_event(target_user, reason: str, staff_member):
	event = ModerationEvent(
		type='ban',
		username=target_user.name,
		discord_id=str(target_user.id),
		created_at=datetime.now(timezone.utc),
		reason=reason,
		staff_id=str(staff_member.id),
		staff_name=staff_member.name
	)
	db.session.add(event)
	_commit_with_retry()
	return event

def _normalized_slash_reason(raison: Optional[str]) -> str:
	if raison is None:
		return "Sans raison"
	s = raison.strip()
	return s if s else "Sans raison"

async def moderation_perform_ban(
	bot,
	guild: discord.Guild,
	staff: discord.Member,
	target_user: discord.User,
	reason: str,
) -> Optional[str]:
	if target_user.id == bot.user.id:
		return "Je ne peux pas me bannir moi-même."
	if staff.id == target_user.id:
		return "Vous ne pouvez pas vous bannir vous-même."
	member = guild.get_member(target_user.id)
	joined_days = None
	if member and member.joined_at:
		delta = datetime.now(timezone.utc) - (member.joined_at if member.joined_at.tzinfo else member.joined_at.replace(tzinfo=timezone.utc))
		joined_days = delta.days
	try:
		await guild.ban(target_user, reason=reason)
	except discord.Forbidden:
		return "Je n'ai pas les permissions nécessaires pour bannir cet utilisateur."
	_create_ban_event(target_user, reason, staff)
	local_now = _to_local(datetime.now(timezone.utc))
	embed = discord.Embed(
		title="🔨 Bannissement",
		description=f"**{target_user.name}** (`{target_user.name}`) a été banni du serveur",
		color=discord.Color.red(),
		timestamp=datetime.now(timezone.utc)
	)
	embed.add_field(name="👤 Utilisateur", value=f"**{target_user.name}**\n`{target_user.id}`", inline=True)
	embed.add_field(name="🛡️ Modérateur", value=f"{staff.name}", inline=True)
	embed.add_field(name="📅 Date et heure", value=local_now.strftime('%d/%m/%Y à %H:%M'), inline=True)
	if joined_days is not None:
		embed.add_field(name="⏱️ Membre depuis", value=format_days_to_age(joined_days), inline=True)
	if reason != "Sans raison":
		embed.add_field(name="📝 Raison", value=reason, inline=False)
	embed.set_footer(text=f"ID: {target_user.id} • Serveur: {guild.name}")
	await send_to_moderation_log_channel(bot, embed)
	return None

async def moderation_perform_kick(
	bot,
	guild: discord.Guild,
	staff: discord.Member,
	target_user: discord.User,
	reason: str,
) -> Optional[str]:
	if target_user.id == bot.user.id:
		return "Je ne peux pas m'expulser moi-même."
	if staff.id == target_user.id:
		return "Vous ne pouvez pas vous expulser vous-même."
	member_obj = guild.get_member(target_user.id)
	if not member_obj:
		return "L'utilisateur n'est pas membre du serveur."
	joined_days = None
	if member_obj.joined_at:
		delta = datetime.now(timezone.utc) - (member_obj.joined_at if member_obj.joined_at.tzinfo else member_obj.joined_at.replace(tzinfo=timezone.utc))
		joined_days = delta.days
	try:
		await guild.kick(member_obj, reason=reason)
	except discord.Forbidden:
		return "Je n'ai pas les permissions nécessaires pour expulser cet utilisateur."
	create = ModerationEvent(
		type='kick',
		username=target_user.name,
		discord_id=str(target_user.id),
		created_at=datetime.now(timezone.utc),
		reason=reason,
		staff_id=str(staff.id),
		staff_name=staff.name
	)
	db.session.add(create)
	_commit_with_retry()
	local_now = _to_local(datetime.now(timezone.utc))
	embed = discord.Embed(
		title="👢 Expulsion",
		description=f"**{target_user.name}** (`{target_user.name}`) a été expulsé du serveur",
		color=discord.Color.orange(),
		timestamp=datetime.now(timezone.utc)
	)
	embed.add_field(name="👤 Utilisateur", value=f"**{target_user.name}**\n`{target_user.id}`", inline=True)
	embed.add_field(name="🛡️ Modérateur", value=f"**{staff.name}**", inline=True)
	embed.add_field(name="📅 Date et heure", value=local_now.strftime('%d/%m/%Y à %H:%M'), inline=True)
	if joined_days is not None:
		embed.add_field(name="⏱️ Membre depuis", value=format_days_to_age(joined_days), inline=True)
	if reason != "Sans raison":
		embed.add_field(name="📝 Raison", value=reason, inline=False)
	embed.set_footer(text=f"ID: {target_user.id} • Serveur: {guild.name}")
	await send_to_moderation_log_channel(bot, embed)
	return None

async def moderation_perform_timeout(
	bot,
	guild: discord.Guild,
	staff: discord.Member,
	target_user: discord.User,
	reason: str,
	timeout_seconds: int,
) -> Optional[str]:
	if target_user.id == bot.user.id:
		return "Je ne peux pas m'exclure temporairement moi-même."
	if staff.id == target_user.id:
		return "Vous ne pouvez pas vous exclure vous-même temporairement."
	member_obj = guild.get_member(target_user.id)
	if not member_obj:
		return "L'utilisateur n'est pas membre du serveur."
	try:
		until = discord.utils.utcnow() + timedelta(seconds=timeout_seconds)
		await member_obj.timeout(until, reason=reason)
		timeout_event = ModerationEvent(
			type='timeout',
			username=target_user.name,
			discord_id=str(target_user.id),
			created_at=datetime.now(timezone.utc),
			reason=reason,
			staff_id=str(staff.id),
			staff_name=staff.name,
			duration=timeout_seconds
		)
		db.session.add(timeout_event)
		_commit_with_retry()
		local_now = _to_local(datetime.now(timezone.utc))
		embed = discord.Embed(
			title="⏱️ Time out",
			description=f"**{target_user.name}** (`{target_user.name}`) a été exclu temporairement ({format_timeout_duration(timeout_seconds)})",
			color=discord.Color.orange(),
			timestamp=datetime.now(timezone.utc)
		)
		embed.add_field(name="👤 Utilisateur", value=f"**{target_user.name}**\n`{target_user.id}`", inline=True)
		embed.add_field(name="🛡️ Modérateur", value=f"**{staff.name}**", inline=True)
		embed.add_field(name="📅 Date et heure", value=local_now.strftime('%d/%m/%Y à %H:%M'), inline=True)
		embed.add_field(name="⏱️ Durée", value=format_timeout_duration(timeout_seconds), inline=True)
		if reason != "Sans raison":
			embed.add_field(name="📝 Raison", value=reason, inline=False)
		embed.set_footer(text=f"ID: {target_user.id} • Serveur: {guild.name}")
		await send_to_moderation_log_channel(bot, embed)
		return None
	except discord.Forbidden:
		return "Je n'ai pas les permissions nécessaires pour exclure cet utilisateur."
	except Exception as e:
		logging.error(f"Erreur lors du timeout de {target_user.name}: {e}")
		return f"Une erreur est survenue lors de l'exclusion : {str(e)}"

async def handle_unban_command(message: Message, bot):
	parts = message.content.split(maxsplit=2)
	if not has_staff_role(message.author.roles):
		await send_access_denied(message.channel)
	elif len(parts) < 2:
		await _send_unban_usage(message.channel)
	else:
		target_user, discord_id, reason = await _parse_unban_target_and_reason(message, bot, parts)
		if not discord_id:
			await _send_unban_invalid_id(message.channel)
		else:
			await _process_unban_success(message, bot, target_user, discord_id, reason)

async def _send_unban_usage(channel):
	embed = discord.Embed(
		title="📋 Utilisation de la commande",
		description="**Syntaxe :** `!unban <discord_id>` ou `!unban #<sanction_id> [raison]`",
		color=discord.Color.blue()
	)
	embed.add_field(name="Exemples", value="• `!unban 123456789012345678`\n• `!unban #5 Appel accepté`", inline=False)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

async def _parse_unban_target_and_reason(message: Message, bot, parts: list):
	reason = parts[2] if len(parts) > 2 else "Sans raison"
	target_user = None
	discord_id = None
	if parts[1].startswith('#'):
		try:
			sanction_id = int(parts[1][1:])
			evt = ModerationEvent.query.filter_by(id=sanction_id, type='ban').first()
			if not evt:
				return None, None, reason
			discord_id = evt.discord_id
			try:
				target_user = await bot.fetch_user(int(discord_id))
			except discord.NotFound:
				pass
		except ValueError:
			return None, None, reason
	else:
		try:
			discord_id = parts[1]
			target_user = await bot.fetch_user(int(discord_id))
		except (ValueError, discord.NotFound):
			return None, None, reason
	return target_user, discord_id, reason

async def _send_unban_invalid_id(channel):
	embed = discord.Embed(
		title="❌ Erreur",
		description="ID Discord invalide ou utilisateur introuvable.",
		color=discord.Color.red()
	)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

async def _process_unban_success(message: Message, bot, target_user, discord_id: str, reason: str):
	try:
		await message.guild.unban(discord.Object(id=int(discord_id)), reason=reason)
	except discord.NotFound:
		embed = discord.Embed(
			title="❌ Erreur",
			description="Cet utilisateur n'est pas banni.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return
	except discord.Forbidden:
		embed = discord.Embed(
			title="❌ Erreur",
			description="Je n'ai pas les permissions nécessaires pour débannir cet utilisateur.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return

	username = target_user.name if target_user else f"ID: {discord_id}"
	create = ModerationEvent(
		type='unban',
		username=username,
		discord_id=discord_id,
		created_at=datetime.now(timezone.utc),
		reason=reason,
		staff_id=str(message.author.id),
		staff_name=message.author.name
	)
	db.session.add(create)
	_commit_with_retry()

	try:
		asyncio.create_task(_send_unban_invite(message, bot, target_user, discord_id))
	except:
		pass

	local_now = _to_local(datetime.now(timezone.utc))
	embed = discord.Embed(
		title="✅ Débannissement",
		description=f"**{username}** (`{username}`) a été débanni du serveur",
		color=discord.Color.green(),
		timestamp=datetime.now(timezone.utc)
	)
	user_display = f"**{target_user.name}**" if target_user else f"**{username}**"
	embed.add_field(name="👤 Utilisateur", value=f"{user_display}\n`{discord_id}`", inline=True)
	embed.add_field(name="🛡️ Modérateur", value=f"**{message.author.name}**", inline=True)
	embed.add_field(name="📅 Date et heure", value=local_now.strftime('%d/%m/%Y à %H:%M'), inline=True)
	if reason != "Sans raison":
		embed.add_field(name="📝 Raison", value=reason, inline=False)
	embed.set_footer(text=f"ID: {discord_id} • Serveur: {message.guild.name}")
	
	await send_to_moderation_log_channel(bot, embed)
	await safe_delete_message(message)

async def _send_unban_invite(message: Message, bot, target_user, discord_id: str):
	try:
		user_obj = target_user or await bot.fetch_user(int(discord_id))
		channel = None
		try:
			channel_id = ConfigurationHelper().getIntValue('welcome_channel_id')
			if channel_id:
				channel = bot.get_channel(channel_id) or message.guild.get_channel(channel_id)
		except:
			pass
		if not channel:
			me = message.guild.me or message.guild.get_member(bot.user.id)
			for ch in message.guild.text_channels:
				try:
					perms = ch.permissions_for(me) if me else None
					if not perms or not perms.create_instant_invite:
						continue
					channel = ch
					break
				except:
					continue
		if not channel:
			channel = message.guild.system_channel or message.channel
		invite = None
		try:
			invite = await channel.create_invite(max_age=86400, max_uses=1, unique=True, reason='Invitation automatique après débannissement')
		except Exception as e:
			logging.warning(f"[UNBAN] Échec création d'invitation sur #{channel and channel.name}: {e}")
			return
		if user_obj and invite:
			try:
				msg = f"Tu as été débanni de {message.guild.name}. Voici une invitation pour revenir : {invite.url}"
				await user_obj.send(msg)
			except Exception as e:
				logging.warning(f"[UNBAN] Impossible d'envoyer un MP à {user_obj} ({user_obj.id}): {e}")
				try:
					await message.author.send(f"Impossible d'envoyer un MP à {user_obj} pour l'unban. Voici l'invitation à lui transmettre : {invite.url}")
				except:
					pass
	except:
		pass

async def handle_ban_list_command(message: Message, bot):
	if not has_staff_role(message.author.roles):
		embed = discord.Embed(
			title="❌ Accès refusé",
			description="Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return

	# Récupérer la liste des bannis
	bans = []
	try:
		async for entry in message.guild.bans(limit=None):
			bans.append(entry)
	except TypeError:
		try:
			bans = await message.guild.bans()
		except Exception:
			bans = []
	except Exception:
		bans = []

	if not bans:
		embed = discord.Embed(
			title="🔨 Utilisateurs bannis",
			description="Aucun utilisateur banni sur ce serveur.",
			color=discord.Color.blue()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		await safe_delete_message(message)
		return

	page = 0
	per_page = 10
	max_page = (len(bans) - 1) // per_page

	def create_banlist_embed(page_num: int):
		start = page_num * per_page
		end = start + per_page
		page_bans = bans[start:end]
		embed = discord.Embed(
			title="🔨 Utilisateurs bannis",
			description=f"Total : {len(bans)} utilisateur(s) banni(s)",
			color=discord.Color.red(),
			timestamp=datetime.now(timezone.utc)
		)
		for entry in page_bans:
			user = entry.user
			reason = entry.reason or 'Sans raison'
			embed.add_field(
				name=f"{user.name} ({user.id})",
				value=f"Raison: {reason}",
				inline=False
			)
		embed.set_footer(text=f"Page {page_num + 1}/{max_page + 1}")
		return embed

	msg = await message.channel.send(embed=create_banlist_embed(page))
	if max_page > 0:
		await msg.add_reaction('⬅️')
		await msg.add_reaction('➡️')
	await msg.add_reaction('❌')

	def check(reaction, user):
		return user == message.author and str(reaction.emoji) in ['⬅️', '➡️', '❌'] and reaction.message.id == msg.id

	while True:
		try:
			reaction, user = await bot.wait_for('reaction_add', timeout=60.0, check=check)
			if str(reaction.emoji) == '❌':
				await msg.delete()
				break
			elif str(reaction.emoji) == '➡️' and page < max_page:
				page += 1
				await msg.edit(embed=create_banlist_embed(page))
			elif str(reaction.emoji) == '⬅️' and page > 0:
				page -= 1
				await msg.edit(embed=create_banlist_embed(page))
			await msg.remove_reaction(reaction, user)
		except Exception:
			break

	try:
		await msg.clear_reactions()
	except Exception:
		pass

	await safe_delete_message(message)

async def handle_staff_help_command(message: Message, bot):
	is_staff = has_staff_role(message.author.roles)
	
	embed = discord.Embed(
		title="📚 Aide - Commandes disponibles",
		description="Liste de toutes les commandes disponibles",
		color=discord.Color.blurple(),
		timestamp=datetime.now(timezone.utc)
	)
	embed.set_thumbnail(url=bot.user.display_avatar.url)
	embed.set_footer(text=f"Demandé par {message.author.name}")

	public_commands = []
	
	if ConfigurationHelper().getValue('proton_db_enable_enable'):
		public_commands.append(
			"**🎮 ProtonDB**\n"
			"• `!protondb nom du jeu` ou `!pdb nom du jeu`\n"
			"Recherche un jeu sur ProtonDB pour vérifier sa compatibilité Linux\n"
			"Ex: `!pdb Elden Ring`"
		)
	
	from database.models import Commande
	custom_commands = Commande.query.filter_by(discord_enable=True).all()
	if custom_commands:
		commands_list = []
		for cmd in custom_commands:
			commands_list.append(f"• `{cmd.trigger}`")
		custom_text = "\n".join(commands_list[:10])
		if len(custom_commands) > 10:
			custom_text += f"\n*... et {len(custom_commands) - 10} autres*"
		public_commands.append(f"**🤖 Commandes personnalisées**\n{custom_text}")
	
	if public_commands:
		for cmd_text in public_commands:
			embed.add_field(name="\u200b", value=cmd_text, inline=False)
	else:
		embed.add_field(
			name="📝 Commandes publiques",
			value="Aucune commande publique configurée pour le moment.",
			inline=False
		)
	
	if is_staff:
		embed.add_field(
			name="━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
			value="**🛠️ COMMANDES STAFF**",
			inline=False
		)
		
		if ConfigurationHelper().getValue('moderation_enable'):
			value = (
				"**Avertissements:**\n"
				"• `!warn @utilisateur raison`\n"
				"  *Alias: !averto, !av, !avertissement*\n"
				"  Donne un avertissement\n"
				"• `!warn @utilisateur raison --to durée`\n"
				"  Avertissement + time out temporaire\n\n"
				"**Time out (sans avertissement):**\n"
				"• `/timeout` — commande slash (membre, durée, raison optionnelle)\n"
				"• Clic droit sur un message → Applications → **Exclure temporairement**\n"
				"  *Durées: 10s, 5m, 1h, 2j*\n\n"
				"**Gestion:**\n"
				"• `!delaverto id` - Supprime un événement\n"
				"• `!warnings [@utilisateur]` - Liste les événements\n\n"
				"**Exemples:**\n"
				"`!warn @User Spam`\n"
				"`!warn @User Flood --to 10m` (averto + timeout)\n"
				"`/timeout` : membre + durée + raison (timeout seul)\n"
				"`!warnings @User`"
			)
			embed.add_field(name="⚠️ Avertissements & Time out", value=value, inline=False)
			embed.add_field(
				name="🔎 Inspection",
				value=("• `!inspect @utilisateur` ou `!inspect id`\n"
						"Affiche les infos détaillées et l'historique de modération\n"
						"Ex: `!inspect @User`"),
				inline=False
			)

		if ConfigurationHelper().getValue('moderation_ban_enable'):
			value = (
				"• `/ban` — bannit un membre (raison optionnelle)\n"
				"• Clic droit sur un message → Applications → **Bannir l'auteur**\n"
				"• `!unban discord_id` ou `!unban #sanction_id raison`\n"
				"  Révoque le ban et envoie une invitation\n"
				"• `!banlist`\n"
				"  Affiche la liste des utilisateurs bannis\n"
				"Exemples:\n"
				"`/ban` sur un membre avec raison\n"
				"`!unban 123456789012345678 Erreur de modération`\n"
				"`!unban #5 Appel accepté`"
			)
			embed.add_field(name="🔨 Bannissement", value=value, inline=False)

		if ConfigurationHelper().getValue('moderation_kick_enable'):
			value = (
				"• `/kick` — expulse un membre (raison optionnelle)\n"
				"• Clic droit sur un message → Applications → **Expulser l'auteur**"
			)
			embed.add_field(name="👢 Expulsion", value=value, inline=False)
		
		embed.add_field(
			name="💬 Autres",
			value=(
				"• `!say #channel message`\n"
				"  Envoie un message en tant que bot\n"
				"  Ex: `!say #annonces Nouvelle fonctionnalité !`\n\n"
				"• `!transfert #canal message_id [raison]`\n"
				"  Transfère un message vers un autre canal\n"
				"  *Alias: !transfer, !move*\n"
				"  Ex: `!transfert #entraide 123456789012345678`\n"
				"  Ex: `!transfert #général https://discord.com/channels/.../...`\n"
				"  Le message sera envoyé comme si c'était l'auteur original\n"
				"  Supporte les canaux textuels, threads et forums (crée un post)\n"
				"  Pour les forums, la raison devient le titre du post"
			),
			inline=False
		)

	try:
		sent = await message.channel.send(embed=embed)
		if is_staff:
			asyncio.create_task(delete_after_delay(sent))
	except Exception:
		pass
	await safe_delete_message(message)

def format_days_to_age(days: int) -> str:
	if days >= 365:
		years = days // 365
		remaining_days = days % 365
		if remaining_days > 0:
			return f"{years} an{'s' if years > 1 else ''} et {remaining_days} jour{'s' if remaining_days > 1 else ''}"
		return f"{years} an{'s' if years > 1 else ''}"
	return f"{days} jour{'s' if days > 1 else ''}"

async def get_member_join_info(guild, member_id: int):
	member = guild.get_member(member_id)
	if not member or not member.joined_at:
		return None, None
	
	join_date = member.joined_at
	days_on_server = (datetime.now(timezone.utc) - join_date).days
	return join_date, days_on_server

def get_account_age(user):
	if not user.created_at:
		return None
	account_age = (datetime.now(timezone.utc) - user.created_at).days
	return account_age

def get_user_moderation_history(discord_id: str):
	events = ModerationEvent.query.filter_by(discord_id=discord_id).order_by(ModerationEvent.created_at.desc()).all()
	
	warnings = [e for e in events if e.type == 'warning']
	kicks = [e for e in events if e.type == 'kick']
	bans = [e for e in events if e.type == 'ban']
	
	return warnings, kicks, bans

async def send_inspect_usage(channel):
	embed = discord.Embed(
		title="📋 Utilisation de la commande",
		description="**Syntaxe :** `!inspect @utilisateur` ou `!inspect <id>`",
		color=discord.Color.blue()
	)
	embed.add_field(name="Exemples", value="• `!inspect @User`\n• `!inspect 123456789012345678`", inline=False)
	msg = await channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(msg))

async def parse_target_user(message: Message, bot, parts: list):
	if message.mentions:
		return message.mentions[0]
	
	try:
		user_id = int(parts[1])
		return await bot.fetch_user(user_id)
	except (ValueError, discord.NotFound):
		return None

def create_inspect_embed(user, member, join_date, days_on_server, account_age, warnings, kicks, bans, invite_info):
	embed = discord.Embed(
		title=f"🔍 Inspection de {user.name}",
		color=discord.Color.blue(),
		timestamp=datetime.now(timezone.utc)
	)
	
	embed.set_thumbnail(url=user.display_avatar.url)
	embed.add_field(name="👤 Utilisateur", value=f"**{user.name}**\n`{user.id}`", inline=True)
	
	if account_age is not None:
		embed.add_field(
			name="📅 Compte créé",
			value=f"{_to_local(user.created_at).strftime('%d/%m/%Y')}\n({format_days_to_age(account_age)})",
			inline=True
		)
	
	if member and join_date:
		embed.add_field(
			name="📥 Rejoint le serveur",
			value=f"{_to_local(join_date).strftime('%d/%m/%Y à %H:%M')}\n({format_days_to_age(days_on_server)})",
			inline=True
		)
	
	if invite_info:
		embed.add_field(name="🎫 Invitation", value=invite_info, inline=True)
	else:
		embed.add_field(name="🎫 Invitation", value="Inconnue", inline=True)
	
	if member and join_date and user.created_at:
		join_dt = join_date if join_date.tzinfo else join_date.replace(tzinfo=timezone.utc)
		created_dt = user.created_at if user.created_at.tzinfo else user.created_at.replace(tzinfo=timezone.utc)
		days_diff = (join_dt - created_dt).days
		if days_diff < 7:
			embed.add_field(
				name="⚠️ Utilisateur suspect",
				value=f"Raison de suspicion: Compte créé {days_diff} jour{'s' if days_diff > 1 else ''} avant de rejoindre le serveur",
				inline=False
			)
	
	warning_text = f"⚠️ **{len(warnings)}** avertissement{'s' if len(warnings) > 1 else ''}"
	kick_text = f"👢 **{len(kicks)}** expulsion{'s' if len(kicks) > 1 else ''}"
	ban_text = f"🔨 **{len(bans)}** ban{'s' if len(bans) > 1 else ''}"
	
	mod_history = f"{warning_text}\n{kick_text}\n{ban_text}"
	
	if warnings or kicks or bans:
		embed.add_field(name="📋 Historique de modération", value=mod_history, inline=False)
		
		if warnings:
			recent_warnings = warnings[:3]
			warnings_detail = "\n".join([
				f"• ID {w.id} - {_to_local(w.created_at).strftime('%d/%m/%Y')} - {w.reason[:50]}{'...' if len(w.reason) > 50 else ''}"
				for w in recent_warnings
			])
			if len(warnings) > 3:
				warnings_detail += f"\n*... et {len(warnings) - 3} autre(s)*"
			embed.add_field(name="⚠️ Derniers avertissements", value=warnings_detail, inline=False)
	else:
		embed.add_field(name="✅ Historique de modération", value="Aucun incident", inline=False)
	
	embed.set_footer(text="Mamie Henriette")
	return embed

async def get_invite_info_for_user(bot, guild, user_id: int):
	try:
		from database import db
		from sqlalchemy import text
		
		result = db.session.execute(
			text("SELECT invite_code, inviter_name FROM member_invites WHERE user_id = :user_id AND guild_id = :guild_id ORDER BY join_date DESC LIMIT 1"),
			{'user_id': str(user_id), 'guild_id': str(guild.id)}
		).fetchone()
		
		if result and result[0]:
			invite_code = result[0]
			inviter_name = result[1]
			display_text = f"`{invite_code}`"
			if inviter_name:
				display_text += f" (créée par {inviter_name})"
			return display_text
		
		return None
	except Exception as e:
		logging.error(f'Erreur lors de la récupération de l\'invitation : {e}')
		return None

async def handle_inspect_command(message: Message, bot):
	if not has_staff_role(message.author.roles):
		await send_access_denied(message.channel)
		return
	
	parts = message.content.split(maxsplit=1)
	
	if len(parts) < 2:
		await send_inspect_usage(message.channel)
		return
	
	target_user = await parse_target_user(message, bot, parts)
	
	if not target_user:
		await send_user_not_found(message.channel)
		return
	
	member = message.guild.get_member(target_user.id)
	join_date, days_on_server = await get_member_join_info(message.guild, target_user.id)
	account_age = get_account_age(target_user)
	warnings, kicks, bans = get_user_moderation_history(str(target_user.id))
	invite_info = await get_invite_info_for_user(bot, message.guild, target_user.id)
	
	embed = create_inspect_embed(
		target_user,
		member,
		join_date,
		days_on_server,
		account_age,
		warnings,
		kicks,
		bans,
		invite_info
	)
	
	await message.channel.send(embed=embed)
	await safe_delete_message(message)

async def handle_say_command(message: Message, bot):
	if not has_staff_role(message.author.roles):
		await send_access_denied(message.channel)
		return
	
	parts = message.content.split(maxsplit=2)
	
	if len(parts) < 3:
		embed = discord.Embed(
			title="📋 Utilisation de la commande",
			description="**Syntaxe :** `!say #channel message` ou `!say <id_salon> message`",
			color=discord.Color.blue()
		)
		embed.add_field(name="Exemples", value="`!say #general Bonjour à tous !`\n`!say 123456789 Annonce importante`", inline=False)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return
	
	target_channel = None
	
	if message.channel_mentions:
		target_channel = message.channel_mentions[0]
	else:
		try:
			channel_id = int(parts[1])
			target_channel = bot.get_channel(channel_id)
			if not target_channel:
				try:
					target_channel = await bot.fetch_channel(channel_id)
				except discord.NotFound:
					pass
		except ValueError:
			pass
	
	if not target_channel:
		embed = discord.Embed(
			title="❌ Erreur",
			description="Vous devez mentionner un canal avec # ou fournir un ID de salon valide.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return
	
	text_to_send = parts[2]
	
	try:
		await target_channel.send(text_to_send)
		await safe_delete_message(message)
	except discord.Forbidden:
		embed = discord.Embed(
			title="❌ Erreur",
			description="Je n'ai pas les permissions pour écrire dans ce canal.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
	except Exception as e:
		logging.error(f"Erreur lors de l'envoi du message: {e}")

async def handle_transfer_command(message: Message, bot):
	if not has_staff_role(message.author.roles):
		await send_access_denied(message.channel)
		return
	
	parts = message.content.split(maxsplit=3)
	
	if len(parts) < 3:
		embed = discord.Embed(
			title="📋 Utilisation de la commande",
			description="**Syntaxe :** `!transfert #canal message_id [raison]` ou `!transfert #canal lien_message [raison]`",
			color=discord.Color.blue()
		)
		embed.add_field(
			name="Exemples", 
			value=(
				"• `!transfert #entraide 123456789012345678`\n"
				"• `!transfert #general https://discord.com/channels/.../...`\n"
				"• `!transfert #entraide 123456789012345678 Message posté dans le mauvais canal`"
			), 
			inline=False
		)
		embed.add_field(
			name="Aliases", 
			value="`!transfert`, `!transfer`, `!move`", 
			inline=False
		)
		embed.add_field(
			name="💡 Astuce", 
			value="Faites un clic droit sur un message → Copier l'identifiant du message, ou copiez le lien du message", 
			inline=False
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return
	
	target_channel = None
	if message.channel_mentions:
		target_channel = message.channel_mentions[0]
	else:
		try:
			channel_id = int(parts[1].strip('<#>'))
			target_channel = bot.get_channel(channel_id)
		except ValueError:
			pass
	
	if not target_channel:
		embed = discord.Embed(
			title="❌ Erreur",
			description="Canal de destination invalide. Mentionnez un canal avec #canal.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return
	
	if not isinstance(target_channel, (TextChannel, ForumChannel, Thread)):
		embed = discord.Embed(
			title="❌ Erreur",
			description=f"Le transfert n'est pas supporté vers ce type de canal. Utilisez un canal textuel, un forum ou un thread.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return
	
	message_id = None
	source_channel = message.channel
	
	if 'discord.com/channels/' in parts[2]:
		try:
			link_parts = parts[2].split('/')
			source_channel_id = int(link_parts[-2])
			message_id = int(link_parts[-1])
			source_channel = bot.get_channel(source_channel_id)
			
			if not source_channel:
				embed = discord.Embed(
					title="❌ Erreur",
					description="Canal source introuvable.",
					color=discord.Color.red()
				)
				msg = await message.channel.send(embed=embed)
				asyncio.create_task(delete_after_delay(msg))
				return
		except (ValueError, IndexError):
			embed = discord.Embed(
				title="❌ Erreur",
				description="Lien de message invalide.",
				color=discord.Color.red()
			)
			msg = await message.channel.send(embed=embed)
			asyncio.create_task(delete_after_delay(msg))
			return
	else:
		try:
			message_id = int(parts[2])
		except ValueError:
			embed = discord.Embed(
				title="❌ Erreur",
				description="ID de message invalide. Utilisez un ID numérique ou un lien Discord.",
				color=discord.Color.red()
			)
			msg = await message.channel.send(embed=embed)
			asyncio.create_task(delete_after_delay(msg))
			return
	
	try:
		original_message = await source_channel.fetch_message(message_id)
	except discord.NotFound:
		embed = discord.Embed(
			title="❌ Erreur",
			description="Message introuvable. Vérifiez l'ID ou le lien.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return
	except discord.Forbidden:
		embed = discord.Embed(
			title="❌ Erreur",
			description="Je n'ai pas la permission d'accéder à ce message.",
			color=discord.Color.red()
		)
		msg = await message.channel.send(embed=embed)
		asyncio.create_task(delete_after_delay(msg))
		return
	
	reason = parts[3] if len(parts) > 3 else "Message posté dans le mauvais canal"
	
	content = original_message.content
	embeds = original_message.embeds
	files_to_send = []
	
	for attachment in original_message.attachments:
		try:
			file_data = await attachment.read()
			files_to_send.append(discord.File(
				fp=io.BytesIO(file_data),
				filename=attachment.filename
			))
		except Exception as e:
			logging.error(f"Erreur lors du téléchargement de la pièce jointe: {e}")
	
	transferred_message = None
	
	if isinstance(target_channel, ForumChannel):
		try:
			post_title = f"{original_message.author.display_name} - "
			if reason and reason != "Message posté dans le mauvais canal":
				remaining_length = 100 - len(post_title)
				post_title += reason[:remaining_length]
			elif content and len(content) > 0:
				remaining_length = 100 - len(post_title)
				post_title += content[:remaining_length]
			else:
				post_title += "Message transféré"
			
			if len(post_title) > 100:
				post_title = post_title[:97] + "..."
			
			transfer_notice = f"**Message original de {original_message.author.mention}**\n"
			transfer_notice += f"*Ce message a été transféré par un membre du staff depuis {source_channel.mention}*\n"
			transfer_notice += "─" * 50 + "\n\n"
			
			full_content = transfer_notice + (content or "")
			
			thread = await target_channel.create_thread(
				name=post_title,
				content=full_content,
				embeds=embeds[:10] if embeds else [],
				files=files_to_send,
				reason=f"Transfert depuis {source_channel.name} par {message.author.name}"
			)
			transferred_message = thread.message
			
		except discord.HTTPException as e:
			logging.error(f"Erreur lors de la création du post dans le forum: {e}")
			embed = discord.Embed(
				title="❌ Erreur",
				description=f"Une erreur est survenue lors de la création du post dans le forum: {str(e)}",
				color=discord.Color.red()
			)
			msg = await message.channel.send(embed=embed)
			asyncio.create_task(delete_after_delay(msg))
			return
			
	else:
		webhooks = await target_channel.webhooks()
		webhook = None
		
		for wh in webhooks:
			if wh.user == bot.user:
				webhook = wh
				break
		
		if not webhook:
			try:
				webhook = await target_channel.create_webhook(name="Mamie Henriette - Transfert")
			except discord.Forbidden:
				embed = discord.Embed(
					title="❌ Erreur",
					description="Je n'ai pas la permission de créer un webhook dans le canal de destination.",
					color=discord.Color.red()
				)
				msg = await message.channel.send(embed=embed)
				asyncio.create_task(delete_after_delay(msg))
				return
		
		try:
			transferred_message = await webhook.send(
				content=content,
				username=original_message.author.display_name,
				avatar_url=original_message.author.display_avatar.url,
				embeds=embeds[:10] if embeds else [],
				files=files_to_send,
				allowed_mentions=discord.AllowedMentions.none(),
				wait=True
			)
		except discord.HTTPException as e:
			logging.error(f"Erreur lors du transfert du message: {e}")
			embed = discord.Embed(
				title="❌ Erreur",
				description=f"Une erreur est survenue lors du transfert du message: {str(e)}",
				color=discord.Color.red()
			)
			msg = await message.channel.send(embed=embed)
			asyncio.create_task(delete_after_delay(msg))
			return
	
	try:
		await original_message.delete()
	except discord.Forbidden:
		logging.warning(f"Impossible de supprimer le message original (ID: {message_id})")
	
	transfer_details = f"De {source_channel.name} vers {target_channel.name}"
	if isinstance(target_channel, ForumChannel):
		transfer_details += " (forum)"
	
	transfer_event = ModerationEvent(
		type='transfer',
		username=original_message.author.name,
		discord_id=str(original_message.author.id),
		created_at=datetime.now(timezone.utc),
		reason=f"{reason} | {transfer_details}",
		staff_id=str(message.author.id),
		staff_name=message.author.name
	)
	db.session.add(transfer_event)
	_commit_with_retry()
	
	local_now = _to_local(datetime.now(timezone.utc))
	destination_info = target_channel.mention if isinstance(target_channel, (TextChannel, Thread)) else f"le forum {target_channel.name}"
	
	embed = discord.Embed(
		title="✅ Message transféré",
		description=f"Le message de **{original_message.author.name}** a été transféré vers {destination_info}",
		color=discord.Color.green(),
		timestamp=datetime.now(timezone.utc)
	)
	embed.add_field(name="👤 Auteur original", value=f"{original_message.author.mention}", inline=True)
	embed.add_field(name="📤 Canal source", value=source_channel.mention, inline=True)
	embed.add_field(name="📥 Canal destination", value=destination_info, inline=True)
	embed.add_field(name="🛡️ Modérateur", value=message.author.mention, inline=True)
	embed.add_field(name="📝 Raison", value=reason, inline=False)
	
	if isinstance(target_channel, ForumChannel):
		embed.add_field(name="ℹ️ Type", value="Nouveau post créé dans le forum", inline=False)
	
	confirmation_msg = await source_channel.send(embed=embed)
	asyncio.create_task(delete_after_delay(confirmation_msg))
	
	log_embed = discord.Embed(
		title="📨 Transfert de message",
		description=f"Un message de **{original_message.author.name}** a été transféré",
		color=discord.Color.blue(),
		timestamp=datetime.now(timezone.utc)
	)
	log_embed.add_field(name="👤 Auteur original", value=f"{original_message.author.name}\n`{original_message.author.id}`", inline=True)
	log_embed.add_field(name="🛡️ Modérateur", value=f"**{message.author.name}**", inline=True)
	log_embed.add_field(name="📅 Date et heure", value=local_now.strftime('%d/%m/%Y à %H:%M'), inline=True)
	log_embed.add_field(name="📤 De", value=source_channel.mention, inline=True)
	log_embed.add_field(name="📥 Vers", value=f"{target_channel.name} ({type(target_channel).__name__})", inline=True)
	log_embed.add_field(name="📝 Raison", value=reason, inline=False)
	
	preview = content[:100] + "..." if content and len(content) > 100 else content
	if preview:
		log_embed.add_field(name="💬 Aperçu du message", value=preview, inline=False)
	
	log_embed.set_footer(text=f"ID Auteur: {original_message.author.id} • Serveur: {message.guild.name}")
	
	await send_to_moderation_log_channel(bot, log_embed)
	
	await safe_delete_message(message)

class TransferReasonModal(Modal, title="Raison du transfert"):
	reason = TextInput(
		label="Raison / Titre du post (forum)",
		placeholder="Pour les forums, ceci devient le titre du post",
		required=False,
		max_length=200,
		style=discord.TextStyle.paragraph
	)
	
	def __init__(self, message: discord.Message, bot, target_channel, selected_tags=None):
		super().__init__()
		self.message = message
		self.bot = bot
		self.target_channel = target_channel
		self.selected_tags = selected_tags or []
	
	async def on_submit(self, interaction: discord.Interaction):
		await interaction.response.defer(ephemeral=True)
		
		reason = self.reason.value.strip() if self.reason.value else "Message posté dans le mauvais canal"
		target_channel = self.target_channel
		source_channel = self.message.channel
		content = self.message.content
		embeds = self.message.embeds
		files_to_send = []
		
		for attachment in self.message.attachments:
			try:
				file_data = await attachment.read()
				files_to_send.append(discord.File(
					fp=io.BytesIO(file_data),
					filename=attachment.filename
				))
			except Exception as e:
				logging.error(f"Erreur lors du téléchargement de la pièce jointe: {e}")
		
		transferred_message = None
		
		if isinstance(target_channel, ForumChannel):
			try:
				post_title = f"{self.message.author.display_name} - "
				if reason and reason != "Message posté dans le mauvais canal":
					remaining_length = 100 - len(post_title)
					post_title += reason[:remaining_length]
				elif content and len(content) > 0:
					remaining_length = 100 - len(post_title)
					post_title += content[:remaining_length]
				else:
					post_title += "Message transféré"
				
				if len(post_title) > 100:
					post_title = post_title[:97] + "..."
				
				transfer_notice = f"**Message original de {self.message.author.mention}**\n"
				transfer_notice += f"*Ce message a été transféré par un membre du staff depuis {source_channel.mention}*\n"
				transfer_notice += "─" * 50 + "\n\n"
				
				full_content = transfer_notice + (content or "")
				
				thread = await target_channel.create_thread(
					name=post_title,
					content=full_content,
					embeds=embeds[:10] if embeds else [],
					files=files_to_send,
					applied_tags=self.selected_tags,
					reason=f"Transfert depuis {source_channel.name} par {interaction.user.name}"
				)
				transferred_message = thread.message
				
				if self.selected_tags:
					tag_names = ", ".join([tag.name for tag in self.selected_tags])
					logging.info(f"Post créé avec les tags: {tag_names}")
				
			except discord.HTTPException as e:
				logging.error(f"Erreur lors de la création du post dans le forum: {e}")
				await interaction.followup.send(f"❌ Erreur lors de la création du post: {str(e)}", ephemeral=True)
				return
		else:
			webhooks = await target_channel.webhooks()
			webhook = None
			
			for wh in webhooks:
				if wh.user == self.bot.user:
					webhook = wh
					break
			
			if not webhook:
				try:
					webhook = await target_channel.create_webhook(name="Mamie Henriette - Transfert")
				except discord.Forbidden:
					await interaction.followup.send("❌ Je n'ai pas la permission de créer un webhook dans le canal de destination.", ephemeral=True)
					return
			
			try:
				transferred_message = await webhook.send(
					content=content,
					username=self.message.author.display_name,
					avatar_url=self.message.author.display_avatar.url,
					embeds=embeds[:10] if embeds else [],
					files=files_to_send,
					allowed_mentions=discord.AllowedMentions.none(),
					wait=True
				)
			except discord.HTTPException as e:
				logging.error(f"Erreur lors du transfert du message: {e}")
				await interaction.followup.send(f"❌ Erreur lors du transfert: {str(e)}", ephemeral=True)
				return
		
		try:
			await self.message.delete()
		except discord.Forbidden:
			logging.warning(f"Impossible de supprimer le message original (ID: {self.message.id})")
		
		transfer_details = f"De {source_channel.name} vers {target_channel.name}"
		if isinstance(target_channel, ForumChannel):
			transfer_details += " (forum)"
		
		transfer_event = ModerationEvent(
			type='transfer',
			username=self.message.author.name,
			discord_id=str(self.message.author.id),
			created_at=datetime.now(timezone.utc),
			reason=f"{reason} | {transfer_details}",
			staff_id=str(interaction.user.id),
			staff_name=interaction.user.name
		)
		db.session.add(transfer_event)
		_commit_with_retry()
		
		destination_info = target_channel.mention if isinstance(target_channel, (TextChannel, Thread)) else f"le forum {target_channel.name}"
		
		await interaction.followup.send(
			f"✅ Message de **{self.message.author.name}** transféré vers {destination_info}",
			ephemeral=True
		)
		
		local_now = _to_local(datetime.now(timezone.utc))
		log_embed = discord.Embed(
			title="📨 Transfert de message",
			description=f"Un message de **{self.message.author.name}** a été transféré",
			color=discord.Color.blue(),
			timestamp=datetime.now(timezone.utc)
		)
		log_embed.add_field(name="👤 Auteur original", value=f"{self.message.author.name}\n`{self.message.author.id}`", inline=True)
		log_embed.add_field(name="🛡️ Modérateur", value=f"**{interaction.user.name}**", inline=True)
		log_embed.add_field(name="📅 Date et heure", value=local_now.strftime('%d/%m/%Y à %H:%M'), inline=True)
		log_embed.add_field(name="📤 De", value=source_channel.mention, inline=True)
		log_embed.add_field(name="📥 Vers", value=f"{target_channel.name} ({type(target_channel).__name__})", inline=True)
		log_embed.add_field(name="📝 Raison", value=reason, inline=False)
		
		preview = content[:100] + "..." if content and len(content) > 100 else content
		if preview:
			log_embed.add_field(name="💬 Aperçu du message", value=preview, inline=False)
		
		log_embed.set_footer(text=f"ID Auteur: {self.message.author.id} • Serveur: {interaction.guild.name}")
		
		await send_to_moderation_log_channel(self.bot, log_embed)

class ForumTagSelect(Select):
	def __init__(self, message: discord.Message, bot, target_channel: ForumChannel):
		options = []
		for tag in target_channel.available_tags[:25]:
			options.append(discord.SelectOption(
				label=tag.name,
				value=str(tag.id),
				emoji=tag.emoji if tag.emoji else None
			))
		
		super().__init__(
			placeholder="Sélectionnez un ou plusieurs tags...",
			options=options,
			min_values=1,
			max_values=min(5, len(options))
		)
		self.message = message
		self.bot = bot
		self.target_channel = target_channel
	
	async def callback(self, interaction: discord.Interaction):
		selected_tags = []
		for tag_id_str in self.values:
			tag_id = int(tag_id_str)
			tag = discord.utils.get(self.target_channel.available_tags, id=tag_id)
			if tag:
				selected_tags.append(tag)
		
		modal = TransferReasonModal(self.message, self.bot, self.target_channel, selected_tags)
		await interaction.response.send_modal(modal)

class ForumTagView(View):
	def __init__(self, message: discord.Message, bot, target_channel: ForumChannel):
		super().__init__(timeout=180)
		self.add_item(ForumTagSelect(message, bot, target_channel))

class TransferChannelSelect(ChannelSelect):
	def __init__(self, message: discord.Message, bot):
		super().__init__(
			placeholder="Sélectionnez le canal de destination...",
			channel_types=[discord.ChannelType.text, discord.ChannelType.forum, discord.ChannelType.public_thread, discord.ChannelType.private_thread],
			min_values=1,
			max_values=1
		)
		self.message = message
		self.bot = bot
	
	async def callback(self, interaction: discord.Interaction):
		selected_channel = self.values[0]
		target_channel = self.bot.get_channel(selected_channel.id)
		
		if not target_channel:
			await interaction.response.send_message("❌ Impossible de récupérer le canal sélectionné.", ephemeral=True)
			return
		
		if isinstance(target_channel, ForumChannel) and target_channel.available_tags:
			view = ForumTagView(self.message, self.bot, target_channel)
			await interaction.response.send_message("🏷️ Sélectionnez un ou plusieurs tags pour ce post :", view=view, ephemeral=True)
		else:
			modal = TransferReasonModal(self.message, self.bot, target_channel)
			await interaction.response.send_modal(modal)

class TransferView(View):
	def __init__(self, message: discord.Message, bot):
		super().__init__(timeout=180)
		self.add_item(TransferChannelSelect(message, bot))

class BanAuthorReasonModal(Modal, title="Bannir l'auteur"):
	reason = TextInput(
		label="Raison",
		placeholder="Optionnel",
		required=False,
		max_length=500,
		style=discord.TextStyle.short,
	)

	def __init__(self, source_message: discord.Message):
		super().__init__()
		self.source_message = source_message

	async def on_submit(self, interaction: discord.Interaction):
		await interaction.response.defer(ephemeral=True)
		bot = interaction.client
		guild = interaction.guild
		if not guild or not isinstance(interaction.user, discord.Member):
			await interaction.followup.send("❌ Action impossible dans ce contexte.", ephemeral=True)
			return
		reason = (self.reason.value or "").strip() or "Via menu contextuel"
		target = self.source_message.author
		err = await moderation_perform_ban(bot, guild, interaction.user, target, reason)
		if err:
			await interaction.followup.send(f"❌ {err}", ephemeral=True)
		else:
			await interaction.followup.send(f"✅ **{target.name}** a été banni.", ephemeral=True)

class KickAuthorReasonModal(Modal, title="Expulser l'auteur"):
	reason = TextInput(
		label="Raison",
		placeholder="Optionnel",
		required=False,
		max_length=500,
		style=discord.TextStyle.short,
	)

	def __init__(self, source_message: discord.Message):
		super().__init__()
		self.source_message = source_message

	async def on_submit(self, interaction: discord.Interaction):
		await interaction.response.defer(ephemeral=True)
		bot = interaction.client
		guild = interaction.guild
		if not guild or not isinstance(interaction.user, discord.Member):
			await interaction.followup.send("❌ Action impossible dans ce contexte.", ephemeral=True)
			return
		reason = (self.reason.value or "").strip() or "Via menu contextuel"
		target = self.source_message.author
		err = await moderation_perform_kick(bot, guild, interaction.user, target, reason)
		if err:
			await interaction.followup.send(f"❌ {err}", ephemeral=True)
		else:
			await interaction.followup.send(f"✅ **{target.name}** a été expulsé.", ephemeral=True)

class TimeoutAuthorModal(Modal, title="Exclure temporairement"):
	duration = TextInput(
		label="Durée (ex. 10m, 1h, 2j)",
		placeholder="10m",
		required=True,
		max_length=12,
		style=discord.TextStyle.short,
	)
	reason = TextInput(
		label="Raison",
		placeholder="Optionnel",
		required=False,
		max_length=500,
		style=discord.TextStyle.short,
	)

	def __init__(self, source_message: discord.Message):
		super().__init__()
		self.source_message = source_message

	async def on_submit(self, interaction: discord.Interaction):
		await interaction.response.defer(ephemeral=True)
		bot = interaction.client
		guild = interaction.guild
		if not guild or not isinstance(interaction.user, discord.Member):
			await interaction.followup.send("❌ Action impossible dans ce contexte.", ephemeral=True)
			return
		timeout_seconds = parse_timeout_from_args(self.duration.value.strip())
		if not timeout_seconds:
			await interaction.followup.send(
				"❌ Durée invalide. Utilisez un format comme `10m`, `1h`, `60s`, `2j`.",
				ephemeral=True,
			)
			return
		reason = (self.reason.value or "").strip() or "Sans raison"
		target = self.source_message.author
		err = await moderation_perform_timeout(bot, guild, interaction.user, target, reason, timeout_seconds)
		if err:
			await interaction.followup.send(f"❌ {err}", ephemeral=True)
		else:
			await interaction.followup.send(
				f"✅ **{target.name}** a été exclu temporairement ({format_timeout_duration(timeout_seconds)}).",
				ephemeral=True,
			)

def _interaction_must_be_staff_member(interaction: discord.Interaction) -> bool:
	return isinstance(interaction.user, discord.Member) and has_staff_role(interaction.user.roles)

@app_commands.command(name="ban", description="Bannit définitivement un membre du serveur.")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(utilisateur="Membre à bannir", raison="Raison enregistrée dans les logs")
async def moderation_slash_ban(interaction: discord.Interaction, utilisateur: discord.Member, raison: Optional[str] = None):
	if not ConfigurationHelper().getValue('moderation_ban_enable'):
		await interaction.response.send_message("❌ La modération (ban) n'est pas activée sur ce serveur.", ephemeral=True)
		return
	if not _interaction_must_be_staff_member(interaction):
		await interaction.response.send_message(
			"❌ Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
			ephemeral=True,
		)
		return
	if utilisateur.bot:
		await interaction.response.send_message("❌ Impossible de bannir un bot avec cette commande.", ephemeral=True)
		return
	reason = _normalized_slash_reason(raison)
	await interaction.response.defer(ephemeral=True)
	err = await moderation_perform_ban(interaction.client, interaction.guild, interaction.user, utilisateur, reason)
	if err:
		await interaction.followup.send(f"❌ {err}", ephemeral=True)
	else:
		await interaction.followup.send(f"✅ **{utilisateur.name}** a été banni.", ephemeral=True)

@app_commands.command(name="kick", description="Expulse un membre du serveur.")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(utilisateur="Membre à expulser", raison="Raison enregistrée dans les logs")
async def moderation_slash_kick(interaction: discord.Interaction, utilisateur: discord.Member, raison: Optional[str] = None):
	if not ConfigurationHelper().getValue('moderation_kick_enable'):
		await interaction.response.send_message("❌ La modération (kick) n'est pas activée sur ce serveur.", ephemeral=True)
		return
	if not _interaction_must_be_staff_member(interaction):
		await interaction.response.send_message(
			"❌ Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
			ephemeral=True,
		)
		return
	if utilisateur.bot:
		await interaction.response.send_message("❌ Impossible d'expulser un bot avec cette commande.", ephemeral=True)
		return
	reason = _normalized_slash_reason(raison)
	await interaction.response.defer(ephemeral=True)
	err = await moderation_perform_kick(interaction.client, interaction.guild, interaction.user, utilisateur, reason)
	if err:
		await interaction.followup.send(f"❌ {err}", ephemeral=True)
	else:
		await interaction.followup.send(f"✅ **{utilisateur.name}** a été expulsé.", ephemeral=True)

@app_commands.command(name="timeout", description="Exclut temporairement un membre (time out).")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.describe(
	utilisateur="Membre à exclure temporairement",
	duree="Durée : 10m, 1h, 60s, 2j, etc.",
	raison="Raison enregistrée dans les logs",
)
async def moderation_slash_timeout(
	interaction: discord.Interaction,
	utilisateur: discord.Member,
	duree: str,
	raison: Optional[str] = None,
):
	if not ConfigurationHelper().getValue('moderation_enable'):
		await interaction.response.send_message("❌ La modération n'est pas activée sur ce serveur.", ephemeral=True)
		return
	if not _interaction_must_be_staff_member(interaction):
		await interaction.response.send_message(
			"❌ Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
			ephemeral=True,
		)
		return
	if utilisateur.bot:
		await interaction.response.send_message("❌ Impossible d'exclure temporairement un bot.", ephemeral=True)
		return
	timeout_seconds = parse_timeout_from_args(duree.strip())
	if not timeout_seconds:
		await interaction.response.send_message(
			"❌ Durée invalide. Exemples : `10m`, `1h`, `60s`, `2j`.",
			ephemeral=True,
		)
		return
	reason = _normalized_slash_reason(raison)
	await interaction.response.defer(ephemeral=True)
	err = await moderation_perform_timeout(
		interaction.client, interaction.guild, interaction.user, utilisateur, reason, timeout_seconds
	)
	if err:
		await interaction.followup.send(f"❌ {err}", ephemeral=True)
	else:
		await interaction.followup.send(
			f"✅ **{utilisateur.name}** a été exclu temporairement ({format_timeout_duration(timeout_seconds)}).",
			ephemeral=True,
		)

@app_commands.context_menu(name="Bannir l'auteur")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
async def moderation_ctx_ban_author(interaction: discord.Interaction, message: discord.Message):
	if not ConfigurationHelper().getValue('moderation_ban_enable'):
		await interaction.response.send_message("❌ La modération (ban) n'est pas activée sur ce serveur.", ephemeral=True)
		return
	if not _interaction_must_be_staff_member(interaction):
		await interaction.response.send_message(
			"❌ Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
			ephemeral=True,
		)
		return
	if message.author.bot:
		await interaction.response.send_message("❌ Impossible de bannir un bot.", ephemeral=True)
		return
	if message.author.id == interaction.client.user.id:
		await interaction.response.send_message("❌ Impossible d'utiliser cette action sur ce message.", ephemeral=True)
		return
	await interaction.response.send_modal(BanAuthorReasonModal(message))

@app_commands.context_menu(name="Expulser l'auteur")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
async def moderation_ctx_kick_author(interaction: discord.Interaction, message: discord.Message):
	if not ConfigurationHelper().getValue('moderation_kick_enable'):
		await interaction.response.send_message("❌ La modération (kick) n'est pas activée sur ce serveur.", ephemeral=True)
		return
	if not _interaction_must_be_staff_member(interaction):
		await interaction.response.send_message(
			"❌ Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
			ephemeral=True,
		)
		return
	if message.author.bot:
		await interaction.response.send_message("❌ Impossible d'expulser un bot.", ephemeral=True)
		return
	if message.author.id == interaction.client.user.id:
		await interaction.response.send_message("❌ Impossible d'utiliser cette action sur ce message.", ephemeral=True)
		return
	await interaction.response.send_modal(KickAuthorReasonModal(message))

@app_commands.context_menu(name="Exclure temporairement")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
async def moderation_ctx_timeout_author(interaction: discord.Interaction, message: discord.Message):
	if not ConfigurationHelper().getValue('moderation_enable'):
		await interaction.response.send_message("❌ La modération n'est pas activée sur ce serveur.", ephemeral=True)
		return
	if not _interaction_must_be_staff_member(interaction):
		await interaction.response.send_message(
			"❌ Vous n'avez pas les permissions nécessaires pour utiliser cette commande.",
			ephemeral=True,
		)
		return
	if message.author.bot:
		await interaction.response.send_message("❌ Impossible d'exclure temporairement un bot.", ephemeral=True)
		return
	if message.author.id == interaction.client.user.id:
		await interaction.response.send_message("❌ Impossible d'utiliser cette action sur ce message.", ephemeral=True)
		return
	await interaction.response.send_modal(TimeoutAuthorModal(message))

@app_commands.context_menu(name="Déplacer le message")
@app_commands.default_permissions(manage_messages=True)
async def transfer_message_context_menu(interaction: discord.Interaction, message: discord.Message):
	if not has_staff_role(interaction.user.roles):
		await interaction.response.send_message("❌ Vous n'avez pas les permissions nécessaires pour utiliser cette commande.", ephemeral=True)
		return
	
	view = TransferView(message, interaction.client)
	await interaction.response.send_message("📨 Sélectionnez le canal de destination :", view=view, ephemeral=True)

