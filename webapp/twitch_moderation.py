from flask import render_template, request, redirect, url_for, jsonify
from webapp import webapp
from webapp.auth import require_page, can_write_page
from database import db
from database.models import Commande, TwitchModerationLog, TwitchLinkFilter, TwitchBannedWord, ModShoutboxMessage
from flask_login import current_user
from database.helpers import ConfigurationHelper
from datetime import datetime, timedelta, timezone


def _format_stream_uptime(started_at_iso):
    """Durée depuis le début du live (texte court pour le panneau)."""
    if not started_at_iso:
        return None
    try:
        s = str(started_at_iso).replace("Z", "+00:00")
        started = datetime.fromisoformat(s)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        sec = int((now - started).total_seconds())
        if sec < 0:
            return None
        h, sec = divmod(sec, 3600)
        m, sec = divmod(sec, 60)
        if h > 0:
            return f"{h}h {m}min"
        if m > 0:
            return f"{m} min"
        return "< 1 min"
    except (ValueError, TypeError):
        return None
import asyncio

MODERATION_COMMANDS = [
    {
        "commands": ["!kick", "!to", "!timeout", "!tm"],
        "usage": "!timeout <viewer> [minutes] [raison]",
        "description": "Ejection temporaire d'un viewer (3 minutes par defaut) avec raison optionnelle",
        "permission": "Moderateur"
    },
    {
        "commands": ["!ban"],
        "usage": "!ban <viewer1> [viewer2] ...",
        "description": "Bannissement d'un ou plusieurs viewers (max 5)",
        "permission": "Moderateur"
    },
    {
        "commands": ["!unban"],
        "usage": "!unban <viewer1> [viewer2] ...",
        "description": "Debannissement d'un ou plusieurs viewers (max 5)",
        "permission": "Moderateur"
    },
    {
        "commands": ["!clean"],
        "usage": "!clean [viewer]",
        "description": "Nettoyage du chat ou des messages d'un viewer",
        "permission": "Moderateur"
    },
    {
        "commands": ["!shieldmode"],
        "usage": "!shieldmode <on/off>",
        "description": "Active/desactive le mode Shield de Twitch",
        "permission": "Moderateur"
    },
    {
        "commands": ["!settitle"],
        "usage": "!settitle <titre>",
        "description": "Changement du titre du live",
        "permission": "Moderateur"
    },
    {
        "commands": ["!setgame", "!setcateg"],
        "usage": "!setgame <jeu>",
        "description": "Changement du jeu/categorie du live",
        "permission": "Moderateur"
    },
    {
        "commands": ["!subon"],
        "usage": "!subon",
        "description": "Activation du mode abonnes uniquement",
        "permission": "Moderateur"
    },
    {
        "commands": ["!suboff"],
        "usage": "!suboff",
        "description": "Desactivation du mode abonnes uniquement",
        "permission": "Moderateur"
    },
    {
        "commands": ["!follon"],
        "usage": "!follon [minutes]",
        "description": "Activation du mode followers-only",
        "permission": "Moderateur"
    },
    {
        "commands": ["!folloff"],
        "usage": "!folloff",
        "description": "Desactivation du mode followers-only",
        "permission": "Moderateur"
    },
    {
        "commands": ["!emoteon"],
        "usage": "!emoteon",
        "description": "Activation du mode emote-only",
        "permission": "Moderateur"
    },
    {
        "commands": ["!emoteoff"],
        "usage": "!emoteoff",
        "description": "Desactivation du mode emote-only",
        "permission": "Moderateur"
    },
    {
        "commands": ["!ann"],
        "usage": "!ann <alias> <on/off/toggle>",
        "description": "Activer/desactiver/inverser une liste d'annonce par alias",
        "permission": "Moderateur"
    },
    {
        "commands": ["!no_game"],
        "usage": "!no_game <on/off>",
        "description": "Desactiver/activer tous les jeux de la chaine",
        "permission": "Moderateur"
    },
    {
        "commands": ["!multitwitch"],
        "usage": "!multitwitch [live1] [live2] ... | auto | reset",
        "description": "Creation d'un lien MultiTwitch. '@' = chaine actuelle, 'auto' = depuis le titre, 'reset' = reinitialiser",
        "permission": "Moderateur (creation) / Tous (affichage)"
    },
    {
        "commands": ["!permit"],
        "usage": "!permit <viewer> [minutes]",
        "description": "Autorise temporairement un viewer a poster un lien (1 minute par defaut)",
        "permission": "Moderateur"
    },
]

TWITCH_PERMISSIONS = {'viewer': 'Tous (viewers)', 'sub': 'Abonnés', 'vip': 'VIP', 'moderator': 'Modérateur'}


@webapp.route("/twitch-moderation")
@require_page("twitch_moderation")
def twitch_moderation():
    custom_commands = Commande.query.filter_by(twitch_enable=True).all()
    logs = TwitchModerationLog.query.order_by(TwitchModerationLog.created_at.desc()).limit(50).all()
    raw_channel = ConfigurationHelper().getValue("twitch_channel") or webapp.config["BOT_STATUS"].get("twitch_channel_name") or "chainesteve"
    twitch_channel = (raw_channel or "").strip().lower() or "chainesteve"
    embed_parent = request.host or "localhost"
    
    # Link filter status
    link_filter_config = TwitchLinkFilter.query.first()
    link_filter_enabled = link_filter_config.enabled if link_filter_config else False
    
    # Banned words
    banned_words = TwitchBannedWord.query.filter_by(enabled=True).all()
    
    # Live status (from BOT_STATUS)
    bot_status = webapp.config.get("BOT_STATUS", {})
    is_live = bot_status.get("twitch_is_live", False)
    viewer_count = bot_status.get("twitch_viewer_count", 0)
    stream_title = bot_status.get("twitch_stream_title", "")
    game_name = bot_status.get("twitch_game_name", "")
    started_at = bot_status.get("twitch_started_at")
    stream_uptime = _format_stream_uptime(started_at) if is_live else None

    return render_template(
        "twitch-moderation.html",
        commands=MODERATION_COMMANDS,
        custom_commands=custom_commands,
        logs=logs,
        twitch_permissions=TWITCH_PERMISSIONS,
        twitch_channel=twitch_channel,
        embed_parent=embed_parent,
        link_filter_enabled=link_filter_enabled,
        banned_words=banned_words,
        is_live=is_live,
        viewer_count=viewer_count,
        stream_title=stream_title,
        game_name=game_name,
        started_at=started_at,
        stream_uptime=stream_uptime,
    )

@webapp.route("/twitch-moderation/logs/clear")
@require_page("twitch_moderation")
def clear_twitch_logs():
    if not can_write_page("twitch_moderation"):
        return render_template("403.html"), 403
    TwitchModerationLog.query.delete()
    db.session.commit()
    return redirect(url_for('twitch_moderation'))

@webapp.route("/twitch-moderation/add", methods=['POST'])
@require_page("twitch_moderation")
def add_twitch_commande():
    if not can_write_page("twitch_moderation"):
        return render_template("403.html"), 403
    trigger = request.form.get('trigger')
    response = request.form.get('response')
    twitch_permission = request.form.get('twitch_permission') or 'viewer'
    if twitch_permission not in TWITCH_PERMISSIONS:
        twitch_permission = 'viewer'
    
    if trigger and response:
        if not trigger.startswith('!'):
            trigger = '!' + trigger
        
        existing = Commande.query.filter_by(trigger=trigger).first()
        if not existing:
            commande = Commande(trigger=trigger, response=response, discord_enable=False, twitch_enable=True, twitch_permission=twitch_permission)
            db.session.add(commande)
            db.session.commit()
    
    return redirect(url_for('twitch_moderation'))

@webapp.route("/twitch-moderation/edit/<int:cmd_id>", methods=['POST'])
@require_page("twitch_moderation")
def edit_twitch_commande(cmd_id):
    if not can_write_page("twitch_moderation"):
        return jsonify({"success": False, "error": "Permission refusée"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Données invalides"}), 400

    commande = Commande.query.get_or_404(cmd_id)

    trigger = (data.get('trigger') or '').strip()
    response = (data.get('response') or '').strip()
    twitch_permission = data.get('twitch_permission', commande.twitch_permission or 'viewer')

    if not trigger or not response:
        return jsonify({"success": False, "error": "Commande et réponse requises"}), 400

    if not trigger.startswith('!'):
        trigger = '!' + trigger

    if twitch_permission not in TWITCH_PERMISSIONS:
        twitch_permission = 'viewer'

    duplicate = Commande.query.filter(Commande.trigger == trigger, Commande.id != cmd_id).first()
    if duplicate:
        return jsonify({"success": False, "error": f"La commande {trigger} existe déjà"}), 409

    commande.trigger = trigger
    commande.response = response
    commande.twitch_permission = twitch_permission
    db.session.commit()

    return jsonify({
        "success": True,
        "command": {
            "id": commande.id,
            "trigger": commande.trigger,
            "response": commande.response,
            "twitch_permission": commande.twitch_permission,
            "permission_label": TWITCH_PERMISSIONS.get(commande.twitch_permission, 'Tous'),
        }
    })


@webapp.route("/twitch-moderation/banned-word/add", methods=['POST'])
@require_page("twitch_moderation")
def add_banned_word():
    if not can_write_page("twitch_moderation"):
        return render_template("403.html"), 403
    
    word = request.form.get('word', '').strip().lower()
    timeout_duration = int(request.form.get('timeout_duration', 60))
    
    if word:
        existing = TwitchBannedWord.query.filter_by(word=word).first()
        if not existing:
            banned_word = TwitchBannedWord(word=word, enabled=True, timeout_duration=timeout_duration)
            db.session.add(banned_word)
            db.session.commit()
    
    return redirect(url_for('twitch_moderation'))

@webapp.route("/twitch-moderation/banned-word/delete/<int:word_id>")
@require_page("twitch_moderation")
def delete_banned_word(word_id):
    if not can_write_page("twitch_moderation"):
        return render_template("403.html"), 403
    
    banned_word = TwitchBannedWord.query.get_or_404(word_id)
    db.session.delete(banned_word)
    db.session.commit()
    
    return redirect(url_for('twitch_moderation'))

@webapp.route("/twitch-moderation/send-message", methods=['POST'])
@require_page("twitch_moderation")
def send_twitch_message():
    if not can_write_page("twitch_moderation"):
        return jsonify({"success": False, "error": "Permission refusée"}), 403
    
    data = request.get_json()
    message = data.get('message', '').strip()
    
    if not message:
        return jsonify({"success": False, "error": "Message vide"}), 400
    
    # Vérifier que le bot Twitch est connecté
    from twitchbot import twitchBot
    if not hasattr(twitchBot, 'chat') or not twitchBot.chat:
        return jsonify({"success": False, "error": "Bot Twitch non connecté"}), 503
    
    # Récupérer le nom du channel
    channel = ConfigurationHelper().getValue('twitch_channel')
    if not channel:
        return jsonify({"success": False, "error": "Channel Twitch non configuré"}), 400
    
    try:
        if not twitchBot._loop:
            return jsonify({"success": False, "error": "Event loop du bot non disponible"}), 503

        async def send_msg():
            await twitchBot.chat.send_message(channel, message)

        future = asyncio.run_coroutine_threadsafe(send_msg(), twitchBot._loop)
        future.result(timeout=10)
        return jsonify({"success": True})
    except TimeoutError:
        return jsonify({"success": False, "error": "Timeout lors de l'envoi"}), 504
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@webapp.route("/twitch-moderation/messages")
@require_page("twitch_moderation")
def get_twitch_messages():
    """Retourne les derniers messages du chat Twitch"""
    bot_status = webapp.config["BOT_STATUS"]
    clear_chat = False
    clear_reason = None

    ended_at_raw = bot_status.get("twitch_ended_at")
    if ended_at_raw:
        try:
            ended_at = datetime.fromisoformat(ended_at_raw)
            if datetime.now(ended_at.tzinfo) >= ended_at + timedelta(hours=1):
                if bot_status.get("twitch_chat_messages"):
                    bot_status["twitch_chat_messages"] = []
                bot_status["twitch_msg_timestamps"] = []
                bot_status["twitch_msg_per_minute"] = 0
                clear_chat = True
                clear_reason = "Chat vidé automatiquement 1h après la fin du live."
        except ValueError:
            pass

    messages = list(bot_status.get("twitch_chat_messages", []))
    return jsonify({
        "messages": messages,
        "msg_per_min": int(bot_status.get("twitch_msg_per_minute", 0)),
        "clear_chat": clear_chat,
        "clear_reason": clear_reason,
    })


@webapp.route("/twitch-moderation/stream-info")
@require_page("twitch_moderation")
def twitch_stream_info():
    """Retourne les infos du stream en cours pour le polling dynamique."""
    bot_status = webapp.config.get("BOT_STATUS", {})
    return jsonify({
        "is_live": bot_status.get("twitch_is_live", False),
        "viewer_count": bot_status.get("twitch_viewer_count", 0),
        "title": bot_status.get("twitch_stream_title", ""),
        "game_name": bot_status.get("twitch_game_name", ""),
        "started_at": bot_status.get("twitch_started_at"),
        "msg_per_min": int(bot_status.get("twitch_msg_per_minute", 0)),
    })

@webapp.route("/twitch-moderation/logs/poll")
@require_page("twitch_moderation")
def poll_twitch_logs():
    """Retourne les logs de modération plus récents qu'un timestamp donné."""
    since_str = request.args.get('since', '')
    since = None
    if since_str:
        try:
            since = datetime.fromisoformat(since_str)
        except ValueError:
            pass

    query = TwitchModerationLog.query.order_by(TwitchModerationLog.created_at.desc())
    if since:
        query = query.filter(TwitchModerationLog.created_at > since)
    logs = query.limit(20).all()

    now = datetime.now().isoformat()
    return jsonify({
        "logs": [
            {
                "id": log.id,
                "action": log.action,
                "moderator": log.moderator,
                "target": log.target or '-',
                "details": log.details or '-',
                "created_at": log.created_at.strftime('%d/%m %H:%M') if log.created_at else '',
                "created_at_iso": log.created_at.isoformat() if log.created_at else '',
            }
            for log in logs
        ],
        "timestamp": now,
        "total": TwitchModerationLog.query.count(),
    })


@webapp.route("/twitch-moderation/execute-action", methods=['POST'])
@require_page("twitch_moderation")
def execute_moderation_action():
    """Exécute une action de modération directement"""
    if not can_write_page("twitch_moderation"):
        return jsonify({"success": False, "error": "Permission refusée"}), 403
    
    data = request.get_json()
    action = data.get('action', '').strip()
    params = data.get('params', {})
    
    if not action:
        return jsonify({"success": False, "error": "Action non spécifiée"}), 400
    
    # Vérifier que le bot Twitch est connecté
    from twitchbot import twitchBot
    if not hasattr(twitchBot, 'chat') or not twitchBot.chat or not hasattr(twitchBot, 'twitch'):
        return jsonify({"success": False, "error": "Bot Twitch non connecté"}), 503
    
    # Récupérer le nom du channel
    channel = ConfigurationHelper().getValue('twitch_channel')
    if not channel:
        return jsonify({"success": False, "error": "Channel Twitch non configuré"}), 400
    
    # Créer un objet ChatMessage simulé pour les commandes qui en ont besoin
    from twitchAPI.chat import ChatMessage
    from types import SimpleNamespace
    
    admin_name = f"WebApp ({current_user.username})"

    if not twitchBot._loop:
        return jsonify({"success": False, "error": "Event loop du bot non disponible"}), 503

    async def execute_action():
        if action == 'timeout':
            from twitchbot.moderation import _get_broadcaster_id, _get_moderator_id, _get_user_id, _log_action
            username = params.get('username', '').strip().lstrip('@')
            duration = int(params.get('duration', 600))
            reason = params.get('reason', 'Timeout')
            
            broadcaster_id = await _get_broadcaster_id(twitchBot.twitch, channel)
            moderator_id = await _get_moderator_id(twitchBot.twitch)
            user_id = await _get_user_id(twitchBot.twitch, username)
            
            if user_id:
                await twitchBot.twitch.ban_user(broadcaster_id, moderator_id, user_id, reason=reason, duration=duration)
                _log_action("timeout", admin_name, username, f"{duration}s - {reason}")
                return {"success": True, "message": f"Timeout de {username} pour {duration}s"}
            return {"success": False, "error": f"Utilisateur {username} introuvable"}
        
        elif action == 'ban':
            from twitchbot.moderation import _get_broadcaster_id, _get_moderator_id, _get_user_id, _log_action
            username = params.get('username', '').strip().lstrip('@')
            reason = params.get('reason', 'Ban')
            
            broadcaster_id = await _get_broadcaster_id(twitchBot.twitch, channel)
            moderator_id = await _get_moderator_id(twitchBot.twitch)
            user_id = await _get_user_id(twitchBot.twitch, username)
            
            if user_id:
                await twitchBot.twitch.ban_user(broadcaster_id, moderator_id, user_id, reason=reason)
                _log_action("ban", admin_name, username, reason)
                return {"success": True, "message": f"Ban de {username}"}
            return {"success": False, "error": f"Utilisateur {username} introuvable"}
        
        elif action == 'clean':
            from twitchbot.moderation import _get_broadcaster_id, _get_moderator_id, _get_user_id, _log_action
            username = params.get('username', '').strip().lstrip('@')
            
            broadcaster_id = await _get_broadcaster_id(twitchBot.twitch, channel)
            moderator_id = await _get_moderator_id(twitchBot.twitch)
            
            if username:
                user_id = await _get_user_id(twitchBot.twitch, username)
                if user_id:
                    await twitchBot.twitch.ban_user(broadcaster_id, moderator_id, user_id, reason="Purge messages", duration=1)
                    _log_action("clean", admin_name, username)
                    return {"success": True, "message": f"Messages de {username} supprimés"}
                return {"success": False, "error": f"Utilisateur {username} introuvable"}
            else:
                await twitchBot.twitch.delete_chat_message(broadcaster_id, moderator_id)
                _log_action("clean", admin_name, None, "Chat complet")
                return {"success": True, "message": "Chat nettoyé"}
        
        elif action == 'permit':
            from database.models import TwitchPermit
            username = params.get('username', '').strip().lstrip('@').lower()
            duration = int(params.get('duration', 60))
            
            expires_at = datetime.now() + timedelta(seconds=duration)
            
            with webapp.app_context():
                existing = TwitchPermit.query.filter_by(username=username).first()
                if existing:
                    existing.expires_at = expires_at
                else:
                    permit = TwitchPermit(username=username, expires_at=expires_at)
                    db.session.add(permit)
                db.session.commit()
            
            return {"success": True, "message": f"Permit accordé à {username} pour {duration//60}min"}
        
        elif action in ['subon', 'suboff', 'emoteon', 'emoteoff']:
            from twitchbot.moderation import _get_broadcaster_id, _get_moderator_id, _log_action
            
            broadcaster_id = await _get_broadcaster_id(twitchBot.twitch, channel)
            moderator_id = await _get_moderator_id(twitchBot.twitch)
            
            if action == 'subon':
                await twitchBot.twitch.update_chat_settings(broadcaster_id, moderator_id, subscriber_mode=True)
                _log_action("subon", admin_name)
                return {"success": True, "message": "Mode abonnés activé"}
            elif action == 'suboff':
                await twitchBot.twitch.update_chat_settings(broadcaster_id, moderator_id, subscriber_mode=False)
                _log_action("suboff", admin_name)
                return {"success": True, "message": "Mode abonnés désactivé"}
            elif action == 'emoteon':
                await twitchBot.twitch.update_chat_settings(broadcaster_id, moderator_id, emote_mode=True)
                _log_action("emoteon", admin_name)
                return {"success": True, "message": "Mode emote activé"}
            elif action == 'emoteoff':
                await twitchBot.twitch.update_chat_settings(broadcaster_id, moderator_id, emote_mode=False)
                _log_action("emoteoff", admin_name)
                return {"success": True, "message": "Mode emote désactivé"}
        
        return {"success": False, "error": f"Action '{action}' non reconnue"}

    try:
        future = asyncio.run_coroutine_threadsafe(execute_action(), twitchBot._loop)
        result = future.result(timeout=15)
        return jsonify(result)
    except TimeoutError:
        return jsonify({"success": False, "error": "Timeout lors de l'exécution"}), 504
    except Exception as e:
        import logging
        logging.error(f"Erreur lors de l'exécution de l'action {action}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================
# Shoutbox modérateurs
# =============================

@webapp.route("/twitch-moderation/shoutbox/send", methods=['POST'])
@require_page("twitch_moderation")
def shoutbox_send():
    if not can_write_page("twitch_moderation"):
        return jsonify({"success": False, "error": "Permission refusée"}), 403

    data = request.get_json()
    message = (data.get('message') or '').strip()[:500]
    if not message:
        return jsonify({"success": False, "error": "Message vide"}), 400

    msg = ModShoutboxMessage(
        author=current_user.username,
        message=message,
        created_at=datetime.now(),
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({"success": True, "id": msg.id})


@webapp.route("/twitch-moderation/shoutbox/transfer", methods=['POST'])
@require_page("twitch_moderation")
def shoutbox_transfer():
    if not can_write_page("twitch_moderation"):
        return jsonify({"success": False, "error": "Permission refusée"}), 403

    data = request.get_json() or {}
    username = (data.get('username') or '').strip().lstrip('@')
    message = (data.get('message') or '').strip()
    if not username or not message:
        return jsonify({"success": False, "error": "Données incomplètes"}), 400

    text = f"@{username}: {message}"
    msg = ModShoutboxMessage(
        author=current_user.username,
        message=text[:500],
        created_at=datetime.now(),
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({"success": True, "id": msg.id})


@webapp.route("/twitch-moderation/shoutbox/messages")
@require_page("twitch_moderation")
def shoutbox_messages():
    since_str = request.args.get('since', '')
    since = None
    if since_str:
        try:
            since = datetime.fromisoformat(since_str)
        except ValueError:
            pass

    chat_query = ModShoutboxMessage.query
    log_query = TwitchModerationLog.query
    if since:
        chat_query = chat_query.filter(ModShoutboxMessage.created_at > since)
        log_query = log_query.filter(TwitchModerationLog.created_at > since)

    chat_msgs = chat_query.order_by(ModShoutboxMessage.created_at.desc()).limit(100).all()
    log_msgs = log_query.order_by(TwitchModerationLog.created_at.desc()).limit(100).all()

    items = []
    for m in chat_msgs:
        items.append({
            "type": "message",
            "id": f"msg-{m.id}",
            "author": m.author,
            "text": m.message,
            "created_at": m.created_at.isoformat() if m.created_at else '',
        })
    for log in log_msgs:
        items.append({
            "type": "sanction",
            "id": f"log-{log.id}",
            "action": log.action,
            "moderator": log.moderator,
            "target": log.target or '',
            "details": log.details or '',
            "created_at": log.created_at.isoformat() if log.created_at else '',
        })

    items.sort(key=lambda x: x["created_at"])
    items = items[-100:]

    return jsonify({
        "items": items,
        "timestamp": datetime.now().isoformat(),
        "online_users": _get_online_users(),
    })


def _get_online_users():
    heartbeats = webapp.config["BOT_STATUS"].get("shoutbox_heartbeats", {})
    cutoff = datetime.now() - timedelta(seconds=15)
    return sorted(u for u, t in heartbeats.items() if t > cutoff)


@webapp.route("/twitch-moderation/shoutbox/heartbeat", methods=['POST'])
@require_page("twitch_moderation")
def shoutbox_heartbeat():
    hb = webapp.config["BOT_STATUS"].setdefault("shoutbox_heartbeats", {})
    hb[current_user.username] = datetime.now()
    return jsonify({"online_users": _get_online_users()})


@webapp.route("/twitch-moderation/shoutbox/clear")
@require_page("twitch_moderation")
def shoutbox_clear():
    if not can_write_page("twitch_moderation"):
        return jsonify({"success": False, "error": "Permission refusée"}), 403
    ModShoutboxMessage.query.delete()
    db.session.commit()
    return jsonify({"success": True})


@webapp.route("/twitch-moderation/shoutbox/popout")
@require_page("twitch_moderation")
def shoutbox_popout():
    return render_template("shoutbox-popout.html")
