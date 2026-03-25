from collections import defaultdict
from datetime import datetime

from flask import redirect, render_template, request, url_for
from sqlalchemy import text

from webapp import webapp
from webapp.auth import require_page
from database import db
from database.models import GuildMemberStats, ModerationEvent
from discordbot import bot
from discordbot.member_stats import get_discord_members_snapshot_sync


def _format_voice_seconds(sec: int) -> str:
	if sec <= 0:
		return "0 min"
	h, sec = divmod(sec, 3600)
	m, sec = divmod(sec, 60)
	if h:
		return f"{h}h {m}min"
	if m:
		return f"{m}min"
	return f"{sec}s"


def _event_to_row(e: ModerationEvent) -> dict:
	return {
		"type": e.type or "—",
		"created_at": e.created_at.strftime("%d/%m/%Y %H:%M") if e.created_at else "—",
		"reason": (e.reason or "")[:500],
		"staff_name": e.staff_name or "—",
		"duration": e.duration,
	}


def _load_latest_invites(guild_id_str: str) -> dict[str, dict]:
	rows = db.session.execute(
		text("""
			SELECT user_id, invite_code, inviter_name, join_date FROM (
				SELECT user_id, invite_code, inviter_name, join_date,
					ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY join_date DESC, id DESC) AS rn
				FROM member_invites WHERE guild_id = :gid
			) WHERE rn = 1
		"""),
		{"gid": guild_id_str},
	).mappings().all()
	out = {}
	for r in rows:
		jd = r["join_date"]
		out[str(r["user_id"])] = {
			"invite_code": r["invite_code"] or "—",
			"inviter_name": r["inviter_name"] or "—",
			"join_date": jd.strftime("%d/%m/%Y %H:%M") if jd else "—",
		}
	return out


def _load_sanctions_by_user(user_ids: list[str]) -> dict[str, list]:
	by_user: dict[str, list] = defaultdict(list)
	chunk = 400
	for i in range(0, len(user_ids), chunk):
		part = user_ids[i : i + chunk]
		if not part:
			continue
		q = ModerationEvent.query.filter(ModerationEvent.discord_id.in_(part))
		for e in q.all():
			by_user[e.discord_id].append(e)
	for uid in by_user:
		by_user[uid].sort(key=lambda x: x.created_at or datetime.min, reverse=True)
	return by_user


@webapp.route("/discord-members")
@webapp.route("/discord-members.html")
def discord_members_redirect():
	"""Redirection : l’URL réelle de la page est /discord-membres (route Flask, pas un fichier .html)."""
	return redirect(url_for("discord_members", **request.args), code=302)


@webapp.route("/discord-membres")
@require_page("discord_members")
def discord_members():
	status = webapp.config.get("BOT_STATUS", {})
	bot_connected = bool(status.get("discord_connected"))

	guild_param = request.args.get("guild_id", type=int)

	ok, err, payload = get_discord_members_snapshot_sync(bot, guild_id=guild_param)

	guild_choices = payload.get("guilds") if payload else None
	if not ok and guild_choices:
		return render_template(
			"discord_members.html",
			bot_connected=bot_connected,
			load_error=err,
			guild_choices=guild_choices,
			guild_name=None,
			members=[],
		)

	if not ok:
		return render_template(
			"discord_members.html",
			bot_connected=bot_connected,
			load_error=err or "Impossible de charger les membres.",
			guild_choices=None,
			guild_name=None,
			members=[],
		)

	guild_id_str = payload["guild_id"]
	guild_name = payload["guild_name"]
	raw_members = payload["members"]
	user_ids = [m["id"] for m in raw_members]

	stats_map = {
		r.user_id: r
		for r in GuildMemberStats.query.filter_by(guild_id=guild_id_str).all()
	}
	invites_map = _load_latest_invites(guild_id_str)
	sanctions_by_user = _load_sanctions_by_user(user_ids)

	members = []
	for m in raw_members:
		uid = m["id"]
		joined_raw = m.get("joined_at")
		joined_display = "—"
		if joined_raw:
			try:
				dt = datetime.fromisoformat(joined_raw.replace("Z", "+00:00"))
				joined_display = dt.strftime("%d/%m/%Y %H:%M") + " UTC"
			except (ValueError, TypeError):
				joined_display = joined_raw
		st = stats_map.get(uid)
		msg_c = st.message_count if st else 0
		voice_s = st.voice_seconds if st else 0
		inv = invites_map.get(uid, {"invite_code": "—", "inviter_name": "—", "join_date": "—"})
		events = sanctions_by_user.get(uid, [])
		sanction_rows = [_event_to_row(e) for e in events]
		search_blob = f"{m.get('display_name') or ''} {m.get('name') or ''} {uid}".lower()
		members.append({
			**m,
			"joined_display": joined_display,
			"search_blob": search_blob,
			"message_count": msg_c,
			"voice_label": _format_voice_seconds(voice_s),
			"voice_seconds": voice_s,
			"invite_code": inv["invite_code"],
			"inviter_name": inv["inviter_name"],
			"invite_join_date": inv["join_date"],
			"sanction_count": len(sanction_rows),
			"sanctions": sanction_rows,
		})

	return render_template(
		"discord_members.html",
		bot_connected=bot_connected,
		load_error=None,
		guild_choices=None,
		guild_name=guild_name,
		guild_id=guild_id_str,
		members=members,
	)
