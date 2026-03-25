"""Compteurs messages / vocal par membre pour la webapp (table guild_member_stats)."""
import asyncio
import logging
from datetime import datetime, timezone

import discord
from sqlalchemy import text

from webapp import webapp
from database import db

logger = logging.getLogger(__name__)

# (guild_id, user_id) -> datetime début session vocale (UTC)
_voice_join_at: dict[tuple[int, int], datetime] = {}


def _now_utc() -> datetime:
	return datetime.now(timezone.utc)


def record_message(guild_id: int, user_id: int) -> None:
	"""Incrémente message_count (hors bots)."""
	try:
		with webapp.app_context():
			db.session.execute(
				text("""
					INSERT INTO guild_member_stats (guild_id, user_id, message_count, voice_seconds, updated_at)
					VALUES (:gid, :uid, 1, 0, CURRENT_TIMESTAMP)
					ON CONFLICT(guild_id, user_id) DO UPDATE SET
						message_count = guild_member_stats.message_count + 1,
						updated_at = CURRENT_TIMESTAMP
				"""),
				{"gid": str(guild_id), "uid": str(user_id)},
			)
			db.session.commit()
	except Exception as e:
		logger.warning("record_message: %s", e)
		try:
			db.session.rollback()
		except Exception:
			pass


def add_voice_seconds(guild_id: int, user_id: int, seconds: int) -> None:
	if seconds <= 0:
		return
	try:
		with webapp.app_context():
			db.session.execute(
				text("""
					INSERT INTO guild_member_stats (guild_id, user_id, message_count, voice_seconds, updated_at)
					VALUES (:gid, :uid, 0, :sec, CURRENT_TIMESTAMP)
					ON CONFLICT(guild_id, user_id) DO UPDATE SET
						voice_seconds = guild_member_stats.voice_seconds + :sec,
						updated_at = CURRENT_TIMESTAMP
				"""),
				{"gid": str(guild_id), "uid": str(user_id), "sec": seconds},
			)
			db.session.commit()
	except Exception as e:
		logger.warning("add_voice_seconds: %s", e)
		try:
			db.session.rollback()
		except Exception:
			pass


def _finalize_voice_session(guild_id: int, user_id: int, end: datetime) -> None:
	key = (guild_id, user_id)
	started = _voice_join_at.pop(key, None)
	if started is None:
		return
	delta = (end - started).total_seconds()
	add_voice_seconds(guild_id, user_id, int(delta))


def on_voice_state_update_track_voice(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
	if member.bot:
		return
	guild_id = member.guild.id
	uid = member.id
	bc = before.channel
	ac = after.channel
	if bc == ac:
		return
	now = _now_utc()
	if bc is not None:
		_finalize_voice_session(guild_id, uid, now)
	if ac is not None:
		_voice_join_at[(guild_id, uid)] = now


async def fetch_guild_members_snapshot(bot: discord.Client, guild_id: int | None) -> tuple[bool, str | None, dict]:
	"""
	Retourne (ok, erreur, payload) avec payload =
	  { guild_id, guild_name, members: [ { id, display_name, name, avatar_url, joined_at, nick, roles } ] }
	"""
	guilds = list(bot.guilds)
	if not guilds:
		return False, "Le bot n'est sur aucun serveur.", {}
	chosen: discord.Guild | None = None
	if guild_id is not None:
		chosen = discord.utils.get(guilds, id=guild_id)
		if chosen is None:
			return False, "Serveur Discord introuvable pour ce bot.", {}
	else:
		if len(guilds) == 1:
			chosen = guilds[0]
		else:
			return (
				False,
				"Plusieurs serveurs : précisez ?guild_id=… dans l'URL.",
				{"guilds": [{"id": g.id, "name": g.name} for g in guilds]},
			)
	try:
		await chosen.chunk(cache=True)
	except Exception as e:
		logger.warning("guild.chunk: %s", e)
	members_out = []
	for m in chosen.members:
		if m.bot:
			continue
		role_list = [r for r in m.roles if r.name != "@everyone"]
		role_list.sort(key=lambda r: r.position, reverse=True)
		roles = ", ".join(r.name for r in role_list[:8])
		if len(role_list) > 8:
			roles += f" (+{len(role_list) - 8})"
		joined = m.joined_at.isoformat() if m.joined_at else None
		members_out.append({
			"id": str(m.id),
			"display_name": m.display_name,
			"name": m.name,
			"avatar_url": m.display_avatar.url if m.display_avatar else "",
			"joined_at": joined,
			"nick": m.nick,
			"roles": roles or "—",
		})
	members_out.sort(key=lambda x: (x["display_name"] or x["name"]).lower())
	return True, None, {
		"guild_id": str(chosen.id),
		"guild_name": chosen.name,
		"members": members_out,
	}


def get_discord_members_snapshot_sync(bot: discord.Client, guild_id: int | None = None, timeout: float = 90.0) -> tuple[bool, str | None, dict]:
	"""Appel thread-safe depuis Flask (run_coroutine_threadsafe sur la boucle du bot)."""
	if bot.loop is None or not bot.is_ready():
		return False, "Bot Discord non connecté.", {}
	try:
		future = asyncio.run_coroutine_threadsafe(
			fetch_guild_members_snapshot(bot, guild_id),
			bot.loop,
		)
		ok, err, payload = future.result(timeout=timeout)
		return ok, err, payload
	except Exception as e:
		logger.error("get_discord_members_snapshot_sync: %s", e)
		return False, str(e), {}
