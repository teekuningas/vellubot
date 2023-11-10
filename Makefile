shell:
	nix develop --extra-experimental-features "nix-command flakes"

check:
	black -t py39 --check main.py
	mypy main.py
	black -t py39 --check src/*.py
	mypy src/*.py

lint:
	black -t py39 main.py
	black -t py39 src/*.py

watch:
	python main.py

watch_parsers:
	python src/parser.py

nix-%:
	nix develop --extra-experimental-features "nix-command flakes" --command $(MAKE) $*
