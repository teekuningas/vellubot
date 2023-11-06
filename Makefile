shell:
	nix develop --extra-experimental-features "nix-command flakes"

check:
	black -t py39 --check bot.py
	mypy bot.py

lint:
	black -t py39 bot.py

watch:
	python bot.py

watch_parsers:
	python bot.py parsers

