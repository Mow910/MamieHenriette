import logging
from typing import Any, List

import discord
from discord import app_commands

from database.helpers import ConfigurationHelper
from protondb import searhProtonDb


def _build_protondb_embed(games: List[Any]) -> discord.Embed:
	total_games = len(games)
	tier_colors = {'platinum': '🟣', 'gold': '🟡', 'silver': '⚪', 'bronze': '🟤', 'borked': '🔴'}
	content = ""
	max_games = 15

	for count, game in enumerate(games[:max_games]):
		g_name = str(game.get('name'))
		g_id = str(game.get('id'))
		tier = str(game.get('tier') or 'N/A').lower()
		tier_icon = tier_colors.get(tier, '⚫')

		new_entry = f"**[{g_name}](<https://www.protondb.com/app/{g_id}>)**\n{tier_icon} Classé **{tier.capitalize()}**"

		ac_status = game.get('anticheat_status')
		if ac_status:
			status_lower = str(ac_status).lower()
			ac_map = {
				'supported': ('✅', 'Supporté'),
				'running': ('⚠️', 'Fonctionne'),
				'broken': ('❌', 'Cassé'),
				'denied': ('🚫', 'Refusé'),
				'planned': ('📅', 'Planifié')
			}
			ac_emoji, ac_label = ac_map.get(status_lower, ('❔', str(ac_status)))
			acs = game.get('anticheats') or []
			ac_list = ', '.join([str(ac) for ac in acs if ac])
			new_entry += f" • [Anti-cheat {ac_emoji} {ac_label}"
			if ac_list:
				new_entry += f" ({ac_list})"
			new_entry += f"](<https://areweanticheatyet.com/game/{g_id}>)"

		new_entry += "\n\n"

		if len(content) + len(new_entry) > 3900:
			rest = len(games) - count
			content += f"*... et {rest} autre{'s' if rest > 1 else ''} jeu{'x' if rest > 1 else ''}*"
			break

		content += new_entry
	else:
		rest = max(0, len(games) - max_games)
		if rest > 0:
			content += f"*... et {rest} autre{'s' if rest > 1 else ''} jeu{'x' if rest > 1 else ''}*"

	return discord.Embed(
		title=f"🎮 Résultats ProtonDB - **{total_games} jeu{'x' if total_games > 1 else ''} trouvé{'s' if total_games > 1 else ''}**",
		description=content,
		color=0x5865F2
	)


async def _protondb_search_followup(interaction: discord.Interaction, query: str) -> None:
	await interaction.response.defer()
	search_msg = None
	try:
		search_msg = await interaction.followup.send(f"🔍 Recherche en cours pour **{query}**...", wait=True)
	except Exception as e:
		logging.error(f"ProtonDB : message de recherche : {e}")

	try:
		games = searhProtonDb(query)
	except Exception as e:
		logging.error(f"ProtonDB : searhProtonDb : {e}")
		games = []

	if search_msg:
		try:
			await search_msg.delete()
		except Exception:
			pass

	if len(games) == 0:
		await interaction.followup.send(
			f"{interaction.user.mention} Je n'ai pas trouvé de jeux correspondant à **{query}**. Es-tu sûr que le jeu est disponible sur Steam ?",
			suppress_embeds=True,
		)
		return

	embed = _build_protondb_embed(games)
	try:
		await interaction.followup.send(embed=embed)
	except Exception as e:
		logging.error(f"ProtonDB : envoi embed : {e}")


@app_commands.command(name="protondb", description="Recherche un jeu sur ProtonDB (compatibilité Linux / Steam).")
@app_commands.describe(jeu="Nom du jeu (ex. Elden Ring)")
async def protondb_slash_command(interaction: discord.Interaction, jeu: str):
	if not ConfigurationHelper().getValue('proton_db_enable_enable'):
		await interaction.response.send_message(
			"❌ La commande ProtonDB n'est pas activée.",
			ephemeral=True,
		)
		return
	query = jeu.strip()
	if not query:
		await interaction.response.send_message(
			"⚠️ Indique le nom d'un jeu.\nExemple : `/protondb jeu:Elden Ring`",
			ephemeral=True,
		)
		return
	await _protondb_search_followup(interaction, query)


@app_commands.context_menu(name="Rechercher sur ProtonDB")
async def protondb_message_context_menu(interaction: discord.Interaction, message: discord.Message):
	if not ConfigurationHelper().getValue('proton_db_enable_enable'):
		await interaction.response.send_message(
			"❌ La commande ProtonDB n'est pas activée.",
			ephemeral=True,
		)
		return
	query = (message.clean_content or "").strip()
	if not query:
		await interaction.response.send_message(
			"❌ Ce message n'a pas de texte exploitable pour une recherche (ou seulement des pièces jointes / mentions vides).",
			ephemeral=True,
		)
		return
	await _protondb_search_followup(interaction, query)
