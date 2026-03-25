import locale
import logging
import threading
import os
import time
from logging.handlers import RotatingFileHandler

from webapp import webapp
from discordbot import bot
from twitchbot import twitchBot


def start_server(): 
    logging.info("Démarrage du serveur web")
    from waitress import serve
    serve(webapp, host="0.0.0.0", port=5000)

def start_discord_bot():
    logging.info("Démarrage du bot Discord")
    with webapp.app_context():
        bot.begin()

def start_twitch_bot():
    logging.info("Démarrage du bot Twitch")
    with webapp.app_context():
        twitchBot.begin()

if __name__ == '__main__':
    # Config logs (console + fichier avec rotation)
    os.makedirs('logs', exist_ok=True)
    log_formatter = logging.Formatter('%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s')
    handlers = []
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(log_formatter)
    handlers.append(stream_handler)
    file_handler = RotatingFileHandler('logs/app.log', maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    handlers.append(file_handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

    # Éviter les doublons de lignes discord (ex. discord.http sans [thread] puis avec) :
    # discord.py peut attacher des handlers avant basicConfig ; on ne garde que la propagation vers la racine.
    for _name in (
        'discord',
        'discord.client',
        'discord.http',
        'discord.gateway',
        'discord.state',
        'discord.webhook',
    ):
        _lg = logging.getLogger(_name)
        _lg.handlers.clear()
        _lg.propagate = True

    # Calmer les logs verbeux de certaines libs si besoin
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('discord').setLevel(logging.WARNING)
    # 429 : en-têtes X-RateLimit-* (voir discordbot/rate_limit_logging.py + doc Discord rate limits)
    logging.getLogger('discord.ratelimit_headers').setLevel(logging.WARNING)

    # Hook exceptions non-capturées (threads inclus)
    def _log_uncaught(exc_type, exc, tb):
        logging.exception('Exception non capturée', exc_info=(exc_type, exc, tb))
    import sys
    sys.excepthook = _log_uncaught
    if hasattr(threading, 'excepthook'):
        def _thread_excepthook(args):
            logging.exception(f"Exception dans le thread {args.thread.name}", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = _thread_excepthook

    locale.setlocale(locale.LC_TIME, 'fr_FR.UTF-8')

    t_discord = threading.Thread(target=start_discord_bot, name='discord-bot')
    t_web = threading.Thread(target=start_server, name='web-server')
    t_twitch = threading.Thread(target=start_twitch_bot, name='twitch-bot')

    # Démarrer Discord en premier : le 429 sur GET /users/@me est souvent un pic au login ;
    # lancer Waitress + Twitch quelques secondes après évite la concurrence au tout début.
    t_discord.start()
    time.sleep(3)
    t_web.start()
    t_twitch.start()
    for job in (t_discord, t_web, t_twitch):
        job.join()
