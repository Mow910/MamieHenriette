# Règlement Discord : rôle d'arrivée à la connexion, rôle validé au clic sur le bouton.
import asyncio
import logging

import discord
from discord import TextChannel
from discord.ui import Button, View

from webapp import webapp
from database import db
from database.helpers import ConfigurationHelper

RULES_BUTTON_CUSTOM_ID = "mamie_rules_accept"
DEFAULT_BUTTON_LABEL = "J'ai lu le règlement"


class AcceptRulesButton(Button):
	def __init__(self, label: str):
		super().__init__(
			style=discord.ButtonStyle.success,
			label=(label or DEFAULT_BUTTON_LABEL)[:80],
			custom_id=RULES_BUTTON_CUSTOM_ID,
		)

	async def callback(self, interaction: discord.Interaction):
		await handle_rules_accept(interaction)


class RulesAcceptView(View):
	def __init__(self, button_label: str):
		super().__init__(timeout=None)
		self.add_item(AcceptRulesButton(button_label))


def register_persistent_rules_view(client: discord.Client) -> None:
	with webapp.app_context():
		label = (ConfigurationHelper().getValue("rules_button_label") or "").strip() or DEFAULT_BUTTON_LABEL
	client.add_view(RulesAcceptView(label))


def _rules_ack_button_success_text(
	validated_role: discord.Role,
	presentation_ch: TextChannel | None,
) -> str:
	base = f"c'est bon 😌 tu as maintenant le rôle **{validated_role.name}**."
	if presentation_ch:
		return f"{base} Tu peux aller te présenter dans {presentation_ch.mention}."
	return base


async def handle_rules_accept(interaction: discord.Interaction) -> None:
	if not interaction.guild:
		await interaction.response.send_message("Action impossible dans ce contexte.", ephemeral=True)
		return

	try:
		member = await interaction.guild.fetch_member(interaction.user.id)
	except (discord.NotFound, discord.HTTPException):
		member = interaction.user if isinstance(interaction.user, discord.Member) else None
	if member is None:
		await interaction.response.send_message("Action impossible dans ce contexte.", ephemeral=True)
		return

	with webapp.app_context():
		config = ConfigurationHelper()
		enabled = config.getValue("rules_ack_enable")
		arrival_id = config.getIntValue("rules_arrival_role_id")
		presentation_id = config.getIntValue("rules_presentation_channel_id")
		validated_id = config.getIntValue("rules_validated_role_id")

	if not enabled:
		await interaction.response.send_message("Cette fonctionnalité est désactivée.", ephemeral=True)
		return

	if not validated_id:
		await interaction.response.send_message("Rôle membre validé non configuré.", ephemeral=True)
		return

	validated_role = interaction.guild.get_role(validated_id)
	if not validated_role:
		await interaction.response.send_message("Rôle membre validé introuvable sur ce serveur.", ephemeral=True)
		return

	arrival_role = interaction.guild.get_role(arrival_id) if arrival_id else None

	presentation_ch = interaction.guild.get_channel(presentation_id)
	presentation_ch = presentation_ch if isinstance(presentation_ch, TextChannel) else None
	success_text = _rules_ack_button_success_text(validated_role, presentation_ch)

	# Le retrait du rôle d'arrivée est la marque persistante de l'acceptation.
	# Le rôle validé peut ensuite être remplacé par le système de présentation ;
	# dans ce cas, un nouveau clic ne doit surtout pas rejouer l'attribution.
	if arrival_role and arrival_role not in member.roles:
		await interaction.response.send_message(
			"Tu as déjà accepté le règlement. Tes rôles ne seront pas modifiés.",
			ephemeral=True,
		)
		return

	if validated_role in member.roles:
		await interaction.response.send_message(success_text, ephemeral=True)
		return

	try:
		await member.add_roles(validated_role, reason="Acceptation du règlement (bouton)")
		if arrival_role and arrival_role in member.roles:
			await member.remove_roles(arrival_role, reason="Passage membre validé après charte")
	except discord.Forbidden:
		await interaction.response.send_message(
			"Je n'ai pas la permission de modifier tes rôles (rôle du bot trop bas ou « Gérer les rôles » manquant).",
			ephemeral=True,
		)
		return
	except discord.HTTPException as e:
		await interaction.response.send_message(f"Erreur Discord : {e}", ephemeral=True)
		return

	await interaction.response.send_message(success_text, ephemeral=True)


async def publish_rules_embed(bot: discord.Client) -> tuple[bool, str]:
	with webapp.app_context():
		config = ConfigurationHelper()
		if not config.getValue("rules_ack_enable"):
			return False, "Activez d'abord « Règlement avec bouton » et enregistrez la configuration."

		channel_id = config.getIntValue("rules_channel_id")
		body = (config.getValue("rules_embed_body") or "").strip()
		title = (config.getValue("rules_embed_title") or "").strip() or "Bienvenue"
		button_label = (config.getValue("rules_button_label") or "").strip() or DEFAULT_BUTTON_LABEL
		old_mid = config.getIntValue("rules_message_id")
		old_ch_id = config.getIntValue("rules_message_channel_id")

	if not channel_id:
		return False, "Choisissez un canal du règlement."
	if not body:
		return False, "Le texte du règlement est vide."

	channel = bot.get_channel(channel_id)
	if not channel or not isinstance(channel, TextChannel):
		return False, "Canal du règlement introuvable."

	if len(body) > 4096:
		body = body[:4093] + "..."

	embed = discord.Embed(title=title, description=body, color=discord.Color.blurple())
	view = RulesAcceptView(button_label)

	try:
		if old_mid and old_ch_id:
			old_ch = bot.get_channel(old_ch_id)
			if old_ch and isinstance(old_ch, TextChannel):
				try:
					old_msg = await old_ch.fetch_message(old_mid)
					await old_msg.delete()
				except (discord.NotFound, discord.Forbidden, discord.HTTPException):
					pass

		msg = await channel.send(embed=embed, view=view)

		with webapp.app_context():
			ConfigurationHelper().createOrUpdate("rules_message_id", str(msg.id))
			ConfigurationHelper().createOrUpdate("rules_message_channel_id", str(channel.id))
			db.session.commit()

		return True, "Message du règlement publié sur Discord."
	except discord.Forbidden:
		return False, "Permission refusée pour envoyer ou supprimer un message dans ce canal."
	except Exception as e:
		logging.exception("publish_rules_embed")
		return False, str(e)


def publish_rules_embed_sync(bot: discord.Client) -> tuple[bool, str]:
	try:
		future = asyncio.run_coroutine_threadsafe(publish_rules_embed(bot), bot.loop)
		return future.result(timeout=30)
	except Exception as e:
		logging.exception("publish_rules_embed_sync")
		return False, str(e)


async def assign_rules_arrival_on_join(bot: discord.Client, member: discord.Member) -> None:
	"""Attribue uniquement le rôle d'arrivée à la connexion (le rôle validé vient du bouton)."""
	with webapp.app_context():
		config = ConfigurationHelper()
		if not config.getValue("rules_ack_enable"):
			return
		arrival_id = config.getIntValue("rules_arrival_role_id")
		validated_id = config.getIntValue("rules_validated_role_id")

	if not arrival_id:
		return

	guild = member.guild
	arrival_role = guild.get_role(arrival_id)
	if not arrival_role:
		logging.warning("assign_rules_arrival_on_join: rôle d'arrivée %s introuvable sur %s", arrival_id, guild.id)
		return

	if validated_id:
		validated_role = guild.get_role(validated_id)
		if validated_role and validated_role in member.roles:
			return

	if arrival_role in member.roles:
		return

	try:
		await member.add_roles(arrival_role, reason="Règlement : rôle d'arrivée à la connexion")
	except discord.Forbidden:
		logging.warning(
			"assign_rules_arrival_on_join: permission refusée pour %s sur %s (hiérarchie des rôles ?)",
			member.id,
			guild.id,
		)
	except discord.HTTPException as e:
		logging.warning("assign_rules_arrival_on_join: %s", e)
