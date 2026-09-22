VENV ?= $(HOME)/.venvs/fcp-mcp-lite
BRIDGE_PORT ?= 9876

.PHONY: mcp-setup patch mcp-doctor logs test

test:
	$(VENV)/bin/python tests/test_cut_spans.py

mcp-setup:
	python3 -m venv $(VENV)
	$(VENV)/bin/python -m pip install -r mcp/requirements.txt

patch:
	./patch/patch_fcp.sh

mcp-doctor:
	./scripts/mcp-doctor.sh

logs:
	tail -f $(HOME)/.local/share/fcp-mcp-lite/calls.jsonl
