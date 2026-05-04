import asyncio
import logging

from flask import render_template, request, redirect, url_for, flash
from webapp import webapp
from webapp.auth import require_page
from database import db
from database.helpers import ConfigurationHelper
from discordbot import bot

RULES_FORM_KEYS = frozenset({
	'rules_channel_id',
	'rules_arrival_role_id',
	'rules_validated_role_id',
	'rules_presentation_channel_id',
	'rules_embed_title',
	'rules_embed_body',
	'rules_button_label',
})

SKIP_FORM_KEYS = frozenset({
	'moderation_staff_role_ids',
	'rules_ack_section_in_form',
	'moderation_roles_in_form',
})


def _form_int_str(raw: str | None) -> str:
	s = (raw or '').strip()
	return s if s.isdigit() else '0'


@webapp.route("/configurations")
@require_page("configurations")
def openConfigurations():
	return render_template("configurations.html", configuration=ConfigurationHelper(), channels=bot.getAllTextChannel(), voice_channels=bot.getAllVoiceChannels(), roles=bot.getAllRoles())

@webapp.route("/configurations/update", methods=['POST'])
@require_page("configurations")
def updateConfiguration():
	checkboxes = {
		'humble_bundle_enable': 'humble_bundle_channel',
		'proton_db_enable_enable': 'proton_db_api_id',
		'proton_db_twitch_enable': 'proton_db_api_id',
		'moderation_enable': 'moderation_staff_role_ids',
		'moderation_ban_enable': 'moderation_staff_role_ids',
		'moderation_kick_enable': 'moderation_staff_role_ids',
		'welcome_enable': 'welcome_channel_id',
		'leave_enable': 'leave_channel_id',
		'auto_rooms_enable': 'auto_rooms_channel_id',
		'twitch_commands_enable': 'twitch_channel',
		'rules_ack_enable': 'rules_channel_id',
	}
	
	# Ne mettre à jour les rôles staff que si la liste a été rendue dans le formulaire.
	# Sinon (bot pas encore prêt, guilds vides), getlist est vide et on écrasait la config en base.
	staff_roles = request.form.getlist('moderation_staff_role_ids')
	if request.form.get('moderation_roles_in_form'):
		if staff_roles:
			ConfigurationHelper().createOrUpdate('moderation_staff_role_ids', ','.join(staff_roles))
		else:
			ConfigurationHelper().createOrUpdate('moderation_staff_role_ids', '')
	
	if request.form.get('rules_ack_section_in_form'):
		ch = ConfigurationHelper()
		ch.createOrUpdate('rules_channel_id', _form_int_str(request.form.get('rules_channel_id')))
		ch.createOrUpdate('rules_arrival_role_id', _form_int_str(request.form.get('rules_arrival_role_id')))
		ch.createOrUpdate('rules_validated_role_id', _form_int_str(request.form.get('rules_validated_role_id')))
		ch.createOrUpdate('rules_presentation_channel_id', _form_int_str(request.form.get('rules_presentation_channel_id')))
		ch.createOrUpdate('rules_embed_title', (request.form.get('rules_embed_title') or '').strip())
		ch.createOrUpdate('rules_embed_body', request.form.get('rules_embed_body') or '')
		ch.createOrUpdate('rules_button_label', (request.form.get('rules_button_label') or '').strip())
	
	for key in request.form:
		if key in SKIP_FORM_KEYS:
			continue
		if request.form.get('rules_ack_section_in_form') and key in RULES_FORM_KEYS:
			continue
		value = request.form.get(key)
		if value and value.strip():
			ConfigurationHelper().createOrUpdate(key, value)
	
	for checkbox, reference_field in checkboxes.items():
		if request.form.get(reference_field) is not None and request.form.get(checkbox) is None:
			ConfigurationHelper().createOrUpdate(checkbox, False)
	
	db.session.commit()
	return redirect(request.referrer)


@webapp.route("/configurations/publish-rules", methods=['POST'])
@require_page("configurations")
def publishRulesMessage():
	from discordbot.rules_ack import publish_rules_embed_sync

	if not bot.loop or bot.loop.is_closed():
		flash("Le bot Discord n'est pas connecté.", "error")
		return redirect(url_for("openConfigurations"))

	ok, msg = publish_rules_embed_sync(bot)
	flash(msg, "success" if ok else "error")
	if not ok:
		logging.warning("publishRulesMessage: %s", msg)
	return redirect(url_for("openConfigurations"))
