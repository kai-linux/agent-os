.PHONY: demo test

demo:
	@./demo.sh

test:
	@.venv/bin/python3 -m pytest tests/ -q
