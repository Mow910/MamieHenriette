# Règlement Discord : embed + bouton persistant, rôles arrivée / validé, promo sur canal présentation.
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


async def handle_rules_accept(interaction: discord.Interaction) -> None:
	if not interaction.guild or not isinstance(interaction.user, discord.Member):
		await interaction.response.send_message("Action impossible dans ce contexte.", ephemeral=True)
		return

	member = interaction.user

	with webapp.app_context():
		config = ConfigurationHelper()
		enabled = config.getValue("rules_ack_enable")
		arrival_id = config.getIntValue("rules_arrival_role_id")
		presentation_id = config.getIntValue("rules_presentation_channel_id")
		validated_id = config.getIntValue("rules_validated_role_id")

	if not enabled:
		await interaction.response.send_message("Cette fonctionnalité est désactivée.", ephemeral=True)
		return

	if not arrival_id:
		await interaction.response.send_message("Rôle d'arrivée non configuré.", ephemeral=True)
		return

	role = interaction.guild.get_role(arrival_id)
	if not role:
		await interaction.response.send_message("Rôle d'arrivée introuvable sur ce serveur.", ephemeral=True)
		return

	if role in member.roles:
		await interaction.response.send_message("Tu as déjà accepté le règlement.", ephemeral=True)
		return

	try:
		await member.add_roles(role, reason="Acceptation du règlement (bouton)")
	except discord.Forbidden:
		await interaction.response.send_message(
			"Je n'ai pas la permission de t'attribuer ce rôle (rôle du bot trop bas ou « Gérer les rôles » manquant).",
			ephemeral=True,
		)
		return
	except discord.HTTPException as e:
		await interaction.response.send_message(f"Erreur Discord : {e}", ephemeral=True)
		return

	presentation_ch = interaction.guild.get_channel(presentation_id)
	presentation_ch = presentation_ch if isinstance(presentation_ch, TextChannel) else None
	validated_role = interaction.guild.get_role(validated_id) if validated_id else None

	base = f"C'est bon 😌 tu as maintenant le rôle **{role.name}**."
	if presentation_ch and validated_role:
		text = (
			f"{base} va te présenter dans {presentation_ch.mention} "
			f"pour recevoir **{validated_role.name}**."
		)
	elif presentation_ch:
		text = f"{base} va te présenter dans {presentation_ch.mention}."
	else:
		text = base

	await interaction.response.send_message(text, ephemeral=True)


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


async def on_presentation_message(bot: discord.Client, message: discord.Message) -> None:
	if message.author.bot:
		return

	with webapp.app_context():
		config = ConfigurationHelper()
		if not config.getValue("rules_ack_enable"):
			return
		presentation_id = config.getIntValue("rules_presentation_channel_id")
		if not presentation_id or message.channel.id != presentation_id:
			return
		arrival_id = config.getIntValue("rules_arrival_role_id")
		validated_id = config.getIntValue("rules_validated_role_id")

	if not validated_id or not arrival_id:
		return

	member = message.author
	if not isinstance(member, discord.Member):
		return

	arrival_role = message.guild.get_role(arrival_id)
	validated_role = message.guild.get_role(validated_id)
	if not validated_role or not arrival_role:
		return
	if arrival_role not in member.roles:
		return
	if validated_role in member.roles:
		return

	try:
		await member.add_roles(validated_role, reason="Présentation dans le canal configuré")
		if arrival_role:
			await member.remove_roles(arrival_role, reason="Membre validé après présentation")
	except (discord.Forbidden, discord.HTTPException) as e:
		logging.warning("on_presentation_message: %s", e)
