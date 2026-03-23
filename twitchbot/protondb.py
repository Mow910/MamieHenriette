import asyncio
import logging

from twitchAPI.chat import ChatMessage

from database.helpers import ConfigurationHelper
from protondb import searhProtonDb
from twitchbot import _user_has_twitch_permission
from webapp import webapp

TIER_ICONS = {
	'platinum': '✅ Platinum',
	'gold': '🥇 Gold',
	'silver': '🥈 Silver',
	'bronze': '🥉 Bronze',
	'borked': '❌ Borked',
	'native': '🐧 Native',
}

AC_ICONS = {
	'supported': '✅',
	'running': '⚠️',
	'broken': '❌',
	'denied': '🚫',
	'planned': '📅',
}


def _format_game_response(game: dict) -> str:
	name = game.get('name', '?')
	tier = (game.get('tier') or '').lower()
	tier_label = TIER_ICONS.get(tier, tier.capitalize() if tier else '?')
	g_id = game.get('id', '')

	parts = [f"[{name}] {tier_label}"]

	ac_status = (game.get('anticheat_status') or '').lower()
	if ac_status:
		ac_icon = AC_ICONS.get(ac_status, '❔')
		acs = game.get('anticheats') or []
		ac_list = ', '.join(str(ac) for ac in acs if ac)
		ac_part = f"Anti-cheat: {ac_icon} {ac_status.capitalize()}"
		if ac_list:
			ac_part += f" ({ac_list})"
		parts.append(ac_part)

	parts.append(f"protondb.com/app/{g_id}")
	return ' | '.join(parts)


async def protondb_command(msg: ChatMessage):
	with webapp.app_context():
		if not ConfigurationHelper().getValue('proton_db_twitch_enable'):
			return
		permission = ConfigurationHelper().getValue('proton_db_twitch_permission') or 'viewer'

	if not _user_has_twitch_permission(msg, permission):
		return

	text = msg.text
	for prefix in ('!protondb', '!pdb'):
		if text.lower().startswith(prefix):
			text = text[len(prefix):]
			break
	name = text.strip()

	if not name:
		await msg.reply(f"@{msg.user.name} Utilisation : !pdb <nom du jeu>  Exemple : !pdb Elden Ring")
		return

	try:
		loop = asyncio.get_event_loop()
		with webapp.app_context():
			games = await loop.run_in_executor(None, searhProtonDb, name)
	except Exception as e:
		logging.error(f'Erreur ProtonDB Twitch pour "{name}": {e}')
		await msg.reply(f"@{msg.user.name} Erreur lors de la recherche ProtonDB.")
		return

	if not games:
		await msg.reply(f"@{msg.user.name} Aucun jeu trouvé pour \"{name}\" sur Steam.")
		return

	for game in games[:3]:
		response = _format_game_response(game)
		if len(response) > 500:
			response = response[:497] + '...'
		await msg.reply(response)
