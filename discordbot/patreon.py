import asyncio
import logging
import re
import xml.etree.ElementTree as ET

import requests
from discord import Client

from database import db
from database.helpers import ConfigurationHelper
from database.models import PatreonPost
from webapp import webapp

logger = logging.getLogger('patreon-notification')
logger.setLevel(logging.INFO)

_patreon_first_check = True


def _get_mention_content() -> str:
	raw = ConfigurationHelper().getValue("patreon_mention")
	if not raw or not str(raw).strip():
		return ""
	parts = []
	for s in str(raw).strip().split(","):
		s = s.strip()
		if s == "everyone":
			parts.append("@everyone")
		elif s == "here":
			parts.append("@here")
		elif s.isdigit():
			parts.append(f"<@&{s}>")
	return " ".join(parts) if parts else ""


def _strip_html(html: str, max_len: int = 300) -> str:
	"""Extrait le texte brut depuis du HTML et tronque."""
	if not html:
		return ""
	text = re.sub(r'<br\s*/?>', '\n', html)
	text = re.sub(r'<[^>]+>', '', text)
	text = re.sub(r'&nbsp;', ' ', text)
	text = re.sub(r'&amp;', '&', text)
	text = re.sub(r'&lt;', '<', text)
	text = re.sub(r'&gt;', '>', text)
	text = re.sub(r'&#\d+;', '', text)
	text = re.sub(r'\n{3,}', '\n\n', text).strip()
	if len(text) > max_len:
		text = text[:max_len].rsplit(' ', 1)[0] + '...'
	return text


def _extract_image(html: str) -> str | None:
	"""Extrait la première URL d'image depuis le contenu HTML."""
	if not html:
		return None
	match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html)
	if match:
		url = match.group(1)
		if url.startswith("http"):
			return url
	return None


def _parse_item(item, creator_name: str) -> dict | None:
	"""Parse un <item> RSS et retourne un dict avec les métadonnées."""
	guid_elem = item.find('guid')
	if guid_elem is None or not guid_elem.text:
		return None
	title_elem = item.find('title')
	link_elem = item.find('link')
	desc_elem = item.find('description')
	pub_elem = item.find('pubDate')
	return {
		'guid': guid_elem.text.strip(),
		'title': title_elem.text if title_elem is not None else 'Nouveau post',
		'link': link_elem.text if link_elem is not None else '',
		'description': desc_elem.text if desc_elem is not None else '',
		'published_at': pub_elem.text if pub_elem is not None else '',
		'creator': creator_name,
	}


def _fetch_rss() -> tuple[list[dict], str] | None:
	"""Fetch le RSS Patreon et retourne (posts, creator_name) ou None."""
	helper = ConfigurationHelper()
	creator = helper.getValue("patreon_creator")
	if not creator or not str(creator).strip():
		return None

	rss_url = f"https://www.patreon.com/rss/{str(creator).strip()}"

	try:
		response = requests.get(rss_url, timeout=15)
	except Exception as e:
		logger.error(f"Patreon: erreur réseau lors de la récupération du RSS: {e}")
		return None

	if response.status_code != 200:
		logger.error(f"Patreon: HTTP {response.status_code} pour {rss_url}")
		return None

	try:
		root = ET.fromstring(response.content)
	except ET.ParseError as e:
		logger.error(f"Patreon: erreur de parsing XML: {e}")
		return None

	creator_name = creator
	channel_elem = root.find('.//channel/title')
	if channel_elem is not None and channel_elem.text:
		creator_name = channel_elem.text

	items = root.findall('.//item')
	posts = []
	for item in items:
		parsed = _parse_item(item, creator_name)
		if parsed:
			posts.append(parsed)

	return (posts, creator_name)


def _build_embed(post: dict):
	import discord

	title = post.get('title') or 'Nouveau post Patreon'
	link = post.get('link') or ''
	description = _strip_html(post.get('description') or '', max_len=350)
	creator = post.get('creator') or 'Patreon'
	image_url = _extract_image(post.get('description') or '')

	helper = ConfigurationHelper()
	try:
		color = int(helper.getValue('patreon_embed_color') or 'F96854', 16)
	except (ValueError, TypeError):
		color = 0xF96854

	embed = discord.Embed(
		title=title,
		url=link if link.startswith("http") else None,
		color=color,
	)

	if description:
		embed.description = description

	embed.set_author(
		name=creator,
		icon_url="https://c5.patreon.com/external/favicon/favicon-32x32.png",
	)

	if image_url:
		embed.set_image(url=image_url)

	embed.set_footer(text="MamieHenriette \u2022 Patreon")

	return embed


async def checkPatreonPosts(bot: Client):
	global _patreon_first_check
	with webapp.app_context():
		helper = ConfigurationHelper()
		if not helper.getValue("patreon_enable"):
			return

		channel_id = helper.getIntValue("patreon_channel_id")
		if not channel_id:
			return

		channel = bot.get_channel(channel_id)
		if not channel:
			logger.warning("Patreon: canal Discord introuvable")
			return

		result = await asyncio.to_thread(_fetch_rss)
		if not result:
			return

		posts, creator_name = result

		if not posts:
			logger.info("Patreon: aucun post trouvé dans le flux RSS")
			return

		if _patreon_first_check:
			logger.info("Patreon: première vérification, synchronisation sans notification")
			for post_data in posts:
				guid = post_data['guid']
				if not PatreonPost.query.get(guid):
					try:
						db.session.add(PatreonPost(
							guid=guid,
							title=post_data['title'],
							link=post_data['link'],
							description=post_data['description'],
							published_at=post_data['published_at'],
							notified=False,
						))
						db.session.commit()
					except Exception as e:
						logger.error(f"Patreon: erreur de synchronisation pour {guid}: {e}")
						db.session.rollback()
			_patreon_first_check = False
			return

		for post_data in posts:
			guid = post_data['guid']

			if PatreonPost.query.get(guid):
				continue

			try:
				embed = _build_embed(post_data)
				content = _get_mention_content()
				await channel.send(content=content or None, embed=embed)
				db.session.add(PatreonPost(
					guid=guid,
					title=post_data['title'],
					link=post_data['link'],
					description=post_data['description'],
					published_at=post_data['published_at'],
					notified=True,
				))
				db.session.commit()
				logger.info(f"Patreon: notification envoyée pour '{post_data['title']}'")
			except Exception as e:
				logger.error(f"Patreon: envoi Discord échoué pour {guid}: {e}")
				db.session.rollback()


async def _send_post_to_discord_async(bot: Client, guid: str) -> tuple[bool, str]:
	"""Envoie un post Patreon sur Discord (appel manuel). Retourne (succès, message)."""
	helper = ConfigurationHelper()
	channel_id = helper.getIntValue("patreon_channel_id")
	if not channel_id:
		return (False, "Aucun canal Discord configuré pour Patreon.")
	channel = bot.get_channel(channel_id)
	if not channel:
		return (False, "Canal Discord introuvable.")

	post_db = PatreonPost.query.get(guid)
	if not post_db:
		return (False, "Post introuvable en base de données.")

	creator = helper.getValue("patreon_creator") or "Patreon"
	# Tenter de récupérer le nom du créateur depuis le RSS
	result = _fetch_rss()
	creator_name = result[1] if result else creator

	post_data = {
		'title': post_db.title or 'Nouveau post',
		'link': post_db.link or '',
		'description': post_db.description or '',
		'creator': creator_name,
	}

	try:
		embed = _build_embed(post_data)
		content = _get_mention_content()
		await channel.send(content=content or None, embed=embed)
		post_db.notified = True
		db.session.commit()
		return (True, "Notification envoyée sur Discord.")
	except Exception as e:
		logger.error(f"Patreon: envoi manuel échoué pour {guid}: {e}")
		db.session.rollback()
		return (False, str(e))


def send_post_to_discord_sync(bot: Client, guid: str) -> tuple[bool, str]:
	"""Appel synchrone pour envoyer un post sur Discord (depuis la webapp)."""
	try:
		future = asyncio.run_coroutine_threadsafe(
			_send_post_to_discord_async(bot, guid),
			bot.loop,
		)
		return future.result(timeout=15)
	except Exception as e:
		logger.error(f"Patreon: send_post_to_discord_sync: {e}")
		return (False, str(e))
