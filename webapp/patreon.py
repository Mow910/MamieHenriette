from flask import render_template, request, redirect, url_for
from urllib.parse import urlencode

from webapp import webapp
from webapp.auth import require_page, can_write_page
from database import db
from database.helpers import ConfigurationHelper
from database.models import PatreonPost
from discordbot import bot
from discordbot.patreon import send_post_to_discord_sync


def _parse_mention_config(raw: str | None) -> tuple[bool, bool, list[str]]:
	everyone, here, role_ids = False, False, []
	if not raw or not str(raw).strip():
		return (everyone, here, role_ids)
	for part in str(raw).strip().split(","):
		part = part.strip()
		if part == "everyone":
			everyone = True
		elif part == "here":
			here = True
		elif part.isdigit():
			role_ids.append(part)
	return (everyone, here, role_ids)


def _format_pub_date(raw: str | None) -> str:
	if not raw or not str(raw).strip():
		return ""
	try:
		from email.utils import parsedate_to_datetime
		dt = parsedate_to_datetime(raw)
		return dt.strftime("%d/%m/%Y %H:%M")
	except Exception:
		return raw[:16] if len(raw or "") >= 16 else (raw or "")


@webapp.route("/patreon")
@require_page("patreon")
def openPatreon():
	helper = ConfigurationHelper()
	channels = bot.getAllTextChannel()
	roles = bot.getAllRoles()
	raw_mention = helper.getValue("patreon_mention")
	mention_everyone, mention_here, mention_role_ids = _parse_mention_config(raw_mention)

	posts = PatreonPost.query.order_by(PatreonPost.published_at.desc()).all()
	for p in posts:
		p.published_formatted = _format_pub_date(p.published_at)

	return render_template(
		"patreon.html",
		configuration=helper,
		channels=channels,
		roles=roles,
		mention_everyone=mention_everyone,
		mention_here=mention_here,
		mention_role_ids=mention_role_ids,
		posts=posts,
	)


@webapp.route("/patreon/update", methods=["POST"])
@require_page("patreon")
def updatePatreon():
	if not can_write_page("patreon"):
		return render_template("403.html"), 403
	helper = ConfigurationHelper()
	enable = request.form.get("patreon_enable") in ("on", "1", "true", "yes")
	creator = (request.form.get("patreon_creator") or "").strip()
	channel_id = request.form.get("patreon_channel_id")

	mention_parts = []
	if request.form.get("patreon_mention_everyone"):
		mention_parts.append("everyone")
	if request.form.get("patreon_mention_here"):
		mention_parts.append("here")
	mention_parts.extend(request.form.getlist("patreon_mention_roles"))

	helper.createOrUpdate("patreon_enable", "true" if enable else "false")
	helper.createOrUpdate("patreon_creator", creator)
	if channel_id:
		try:
			helper.createOrUpdate("patreon_channel_id", str(int(channel_id)))
		except ValueError:
			pass
	helper.createOrUpdate("patreon_mention", ",".join(mention_parts))
	db.session.commit()
	return redirect(url_for("openPatreon") + "?msg=Configuration enregistrée.&type=success")


@webapp.route("/patreon/send", methods=["POST"])
@require_page("patreon")
def sendPatreonToDiscord():
	if not can_write_page("patreon"):
		return render_template("403.html"), 403
	guid = (request.form.get("guid") or "").strip()
	if not guid:
		return redirect(url_for("openPatreon") + "?" + urlencode({"msg": "Post manquant.", "type": "error"}))
	ok, message = send_post_to_discord_sync(bot, guid)
	msg_type = "success" if ok else "error"
	return redirect(url_for("openPatreon") + "?" + urlencode({"msg": message, "type": msg_type}))
