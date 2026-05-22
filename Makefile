.PHONY: install setup test lint smoke scan backtest dashboard paper paper-all kill clean

install:
	python -m venv .venv
	. .venv/bin/activate && pip install -r requirements.txt

setup:
	cp -n .env.example .env || true
	@echo "Edit .env with your keys, then: make smoke"

test:
	pytest tests/ -v

lint:
	ruff check .
	mypy --ignore-missing-imports core risk strategies exchanges backtest

smoke:
	python scripts/smoke_test.py

scan:
	python scripts/scan_markets.py --resolution-window 72 --limit 25

backtest:
	python scripts/backtest.py --strategy resolution_arb --markets 50 --verbose

backtest-large:
	python scripts/backtest.py --strategy resolution_arb --markets 200 --verbose

dashboard:
	streamlit run app.py

paper:
	python scripts/run.py --strategies polymarket_resolution_arb --mode paper

paper-all:
	python scripts/run.py --strategies polymarket_resolution_arb,polymarket_copy_trade,polymarket_cross_platform_arb,polymarket_news_event,solana_jupiter_arb,solana_copy_trade --mode paper

kill:
	python scripts/kill.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	rm -rf data/cache
