import logging
import asyncio
import xml.etree.ElementTree as ET
import requests
import discord

from database import db
from database.models import YouTubeNotification
from webapp import webapp

logger = logging.getLogger('youtube-notification')
logger.setLevel(logging.INFO)

_youtube_first_check = True


async def checkYouTubeVideos():
	global _youtube_first_check
	with webapp.app_context():
		try:
			notifications: list[YouTubeNotification] = YouTubeNotification.query.filter_by(enable=True).all()
			
			for notification in notifications:
				try:
					await _checkChannelVideos(notification, is_first_check=_youtube_first_check)
				except Exception as e:
					logger.error(f"Erreur lors de la vérification de la chaîne {notification.channel_id}: {e}")
					db.session.rollback()
					continue
			
			if _youtube_first_check:
				_youtube_first_check = False
				logger.info("YouTube: première vérification terminée, notifications activées")
		except Exception as e:
			logger.error(f"Erreur lors de la vérification YouTube: {e}")
			db.session.rollback()


def _extract_embed_config(notification: YouTubeNotification) -> dict:
	"""Extrait toutes les valeurs ORM nécessaires à l'envoi dans un dict plain Python.
	Doit être appelé pendant que le contexte Flask est actif."""
	return {
		'notify_channel': notification.notify_channel,
		'message_template': notification.message or '',
		'embed_title': notification.embed_title,
		'embed_description': notification.embed_description,
		'embed_color': notification.embed_color or 'FF0000',
		'embed_footer': notification.embed_footer,
		'embed_author_name': notification.embed_author_name,
		'embed_author_icon': (notification.embed_author_icon or '').strip(),
		'embed_thumbnail': bool(notification.embed_thumbnail),
		'embed_image': bool(notification.embed_image),
	}


async def _checkChannelVideos(notification: YouTubeNotification, is_first_check: bool = False):
	try:
		channel_id = notification.channel_id
		
		rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
		
		response = await asyncio.to_thread(requests.get, rss_url, timeout=10)
		
		if response.status_code != 200:
			logger.error(f"Erreur HTTP {response.status_code} lors de la récupération du RSS pour {channel_id}")
			return
		
		root = ET.fromstring(response.content)
		
		ns = {'atom': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015', 'media': 'http://search.yahoo.com/mrss/'}
		
		entries = root.findall('atom:entry', ns)
		
		if not entries:
			logger.warning(f"Aucune vidéo trouvée dans le RSS pour {channel_id}")
			return
		
		videos = []
		for entry in entries:
			video_id = entry.find('yt:videoId', ns)
			if video_id is None:
				continue
			video_id = video_id.text
			
			title_elem = entry.find('atom:title', ns)
			video_title = title_elem.text if title_elem is not None else 'Sans titre'
			
			link_elem = entry.find('atom:link', ns)
			video_url = link_elem.get('href') if link_elem is not None else f"https://www.youtube.com/watch?v={video_id}"
			
			published_elem = entry.find('atom:published', ns)
			published_at = published_elem.text if published_elem is not None else ''
			
			author_elem = entry.find('atom:author/atom:name', ns)
			channel_name = author_elem.text if author_elem is not None else 'Inconnu'
			
			thumbnail = None
			media_thumbnail = entry.find('media:group/media:thumbnail', ns)
			if media_thumbnail is not None:
				thumbnail = media_thumbnail.get('url')
			
			is_short = False
			if video_title and ('#shorts' in video_title.lower() or '#short' in video_title.lower()):
				is_short = True
			
			video_data = {
				'title': video_title,
				'url': video_url,
				'published': published_at,
				'channel_name': channel_name,
				'thumbnail': thumbnail,
				'is_short': is_short
			}
			
			if notification.video_type == 'all':
				videos.append((video_id, video_data))
			elif notification.video_type == 'short' and is_short:
				videos.append((video_id, video_data))
			elif notification.video_type == 'video' and not is_short:
				videos.append((video_id, video_data))
		
		videos.sort(key=lambda x: x[1]['published'], reverse=True)
		
		# Enregistrer toutes les vidéos du flux dans l'historique (les doublons sont ignorés)
		for vid, vdata in videos:
			_save_video_history(notification.id, vid, vdata, notified=False)
		
		if videos:
			latest_video_id, latest_video = videos[0]
			
			if is_first_check:
				if not notification.last_video_id or notification.last_video_id != latest_video_id:
					logger.info(f"YouTube: synchronisation initiale pour {channel_id}, dernière vidéo: {latest_video_id}")
					notification.last_video_id = latest_video_id
					db.session.commit()
				return
			
			if not notification.last_video_id:
				notification.last_video_id = latest_video_id
				db.session.commit()
				return
			
			if latest_video_id != notification.last_video_id:
				logger.info(f"Nouvelle vidéo détectée: {latest_video_id} pour la chaîne {notification.channel_id}")
				embed_config = _extract_embed_config(notification)
				success = await _notifyVideo(embed_config, latest_video, latest_video_id)
				if success:
					_save_video_history(notification.id, latest_video_id, latest_video, notified=True)
				else:
					logger.warning(f"Notification échouée pour {latest_video_id}, vidéo enregistrée comme non notifiée")
				notification.last_video_id = latest_video_id
				db.session.commit()
				
	except Exception as e:
		logger.error(f"Erreur lors de la vérification des vidéos: {e}")
		db.session.rollback()


def _save_video_history(notification_id: int, video_id: str, video_data: dict, notified: bool):
	"""Enregistre une vidéo dans l'historique (ne fait rien si déjà présente)."""
	from database.models import YouTubeVideoHistory
	try:
		existing = YouTubeVideoHistory.query.filter_by(
			notification_id=notification_id, video_id=video_id
		).first()
		if existing:
			if notified and not existing.notified:
				existing.notified = True
				db.session.commit()
			return
		entry = YouTubeVideoHistory(
			notification_id=notification_id,
			video_id=video_id,
			title=video_data.get('title', 'Sans titre'),
			url=video_data.get('url', f"https://www.youtube.com/watch?v={video_id}"),
			channel_name=video_data.get('channel_name', 'Inconnu'),
			thumbnail=video_data.get('thumbnail'),
			published_at=video_data.get('published', ''),
			is_short=video_data.get('is_short', False),
			notified=notified,
		)
		db.session.add(entry)
		db.session.commit()
	except Exception as e:
		logger.error(f"Erreur lors de l'enregistrement de l'historique vidéo: {e}")
		db.session.rollback()


async def _notifyVideo(embed_config: dict, video_data: dict, video_id: str) -> bool:
	"""Envoie la notification Discord. Retourne True si l'envoi a réussi."""
	from discordbot import bot
	try:
		channel_name = video_data.get('channel_name', 'Inconnu')
		video_title = video_data.get('title', 'Sans titre')
		video_url = video_data.get('url', f"https://www.youtube.com/watch?v={video_id}")
		thumbnail = video_data.get('thumbnail', '')
		published_at = video_data.get('published', '')
		is_short = video_data.get('is_short', False)
		
		message_template = embed_config.get('message_template', '')
		try:
			message = message_template.format(
				channel_name=channel_name or 'Inconnu',
				video_title=video_title or 'Sans titre',
				video_url=video_url,
				video_id=video_id,
				thumbnail=thumbnail or '',
				published_at=published_at or '',
				is_short=is_short
			)
		except (KeyError, AttributeError, ValueError) as e:
			logger.error(f"Erreur de formatage du message: {e}")
			message = f"🎥 Nouvelle vidéo de {channel_name}: [{video_title}]({video_url})"
		
		logger.info(f"Envoi de notification YouTube: {message}")
		return await _sendMessage(embed_config, message, video_url, thumbnail, video_title, channel_name, video_id, published_at, is_short)
		
	except Exception as e:
		logger.error(f"Erreur lors de la notification: {e}")
		return False


def _format_embed_text(text: str, channel_name: str, video_title: str, video_url: str, video_id: str, thumbnail: str, published_at: str, is_short: bool) -> str:
	"""Formate un texte d'embed avec les variables disponibles"""
	if not text:
		return None
	try:
		return text.format(
			channel_name=channel_name or 'Inconnu',
			video_title=video_title or 'Sans titre',
			video_url=video_url,
			video_id=video_id,
			thumbnail=thumbnail or '',
			published_at=published_at or '',
			is_short=is_short
		)
	except KeyError:
		return text


async def _sendMessage(embed_config: dict, message: str, video_url: str, thumbnail: str, video_title: str, channel_name: str, video_id: str, published_at: str, is_short: bool) -> bool:
	"""Envoie le message Discord. Retourne True si l'envoi a réussi."""
	from discordbot import bot
	try:
		discord_channel = bot.get_channel(embed_config['notify_channel'])
		if not discord_channel:
			logger.error(f"Canal Discord {embed_config['notify_channel']} introuvable")
			return False
		
		embed_title_text = _format_embed_text(embed_config['embed_title'], channel_name, video_title, video_url, video_id, thumbnail, published_at, is_short) if embed_config['embed_title'] else video_title
		embed_description = _format_embed_text(embed_config['embed_description'], channel_name, video_title, video_url, video_id, thumbnail, published_at, is_short) if embed_config['embed_description'] else None
		
		try:
			embed_color = int(embed_config['embed_color'], 16)
		except ValueError:
			embed_color = 0xFF0000
		
		embed = discord.Embed(
			title=embed_title_text,
			url=video_url,
			color=embed_color
		)
		
		if embed_description:
			embed.description = embed_description
		
		author_name = _format_embed_text(embed_config['embed_author_name'], channel_name, video_title, video_url, video_id, thumbnail, published_at, is_short) if embed_config['embed_author_name'] else channel_name
		author_icon_raw = embed_config['embed_author_icon']
		author_icon = author_icon_raw if author_icon_raw.startswith(("http://", "https://")) else "https://www.youtube.com/img/desktop/yt_1200.png"
		embed.set_author(name=author_name, icon_url=author_icon)
		
		if embed_config['embed_thumbnail'] and thumbnail:
			embed.set_thumbnail(url=thumbnail)
		
		if embed_config['embed_image'] and thumbnail:
			embed.set_image(url=thumbnail)
		
		if embed_config['embed_footer']:
			footer_text = _format_embed_text(embed_config['embed_footer'], channel_name, video_title, video_url, video_id, thumbnail, published_at, is_short)
			if footer_text:
				embed.set_footer(text=footer_text)
		
		if message and message.strip():
			await discord_channel.send(message, embed=embed)
		else:
			await discord_channel.send(embed=embed)
		logger.info(f"Notification YouTube envoyée avec succès")
		return True
		
	except Exception as e:
		logger.error(f"Erreur lors de l'envoi du message Discord: {e}")
		return False


async def _send_video_notification_async(history_id: int) -> tuple[bool, str]:
	"""Force l'envoi d'une notification pour une vidéo de l'historique. Retourne (succès, message)."""
	from database.models import YouTubeVideoHistory
	with webapp.app_context():
		history = YouTubeVideoHistory.query.get(history_id)
		if not history:
			return (False, "Vidéo introuvable dans l'historique.")
		
		notification = YouTubeNotification.query.get(history.notification_id)
		if not notification:
			return (False, "Notification YouTube associée introuvable.")
		
		embed_config = _extract_embed_config(notification)
		video_data = {
			'title': history.title or 'Sans titre',
			'url': history.url or f"https://www.youtube.com/watch?v={history.video_id}",
			'channel_name': history.channel_name or 'Inconnu',
			'thumbnail': history.thumbnail or '',
			'published': history.published_at or '',
			'is_short': history.is_short,
		}
		
		success = await _notifyVideo(embed_config, video_data, history.video_id)
		if success:
			history.notified = True
			db.session.commit()
			return (True, "Notification envoyée sur Discord.")
		else:
			db.session.rollback()
			return (False, "Échec de l'envoi sur Discord.")


def send_video_notification_sync(history_id: int) -> tuple[bool, str]:
	"""Appel synchrone pour forcer une notification (depuis la webapp)."""
	from discordbot import bot
	try:
		future = asyncio.run_coroutine_threadsafe(
			_send_video_notification_async(history_id),
			bot.loop,
		)
		return future.result(timeout=15)
	except Exception as e:
		logger.error(f"send_video_notification_sync: {e}")
		return (False, str(e))
