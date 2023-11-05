shell:
	nix develop

watch:
	BOT_CHANNEL=#vellumotest BOT_NICKNAME=vellubot BOT_SERVER=irc.libera.chat BOT_PORT=6667 python bot.py
