VENV ?= $(HOME)/.venvs/fcp-mcp-lite
BRIDGE_PORT ?= 9876

.PHONY: mcp-setup patch mcp-doctor logs test ui

UI_PORT ?= 8765

test:
	$(VENV)/bin/python tests/test_cut_spans.py
	$(VENV)/bin/python tests/test_safety.py
	$(VENV)/bin/python tests/test_story.py
	$(VENV)/bin/python tests/test_lineops.py
	$(VENV)/bin/python tests/test_editor.py

mcp-setup:
	python3 -m venv $(VENV)
	$(VENV)/bin/python -m pip install -r mcp/requirements.txt

patch:
	./patch/patch_fcp.sh

mcp-doctor:
	./scripts/mcp-doctor.sh

logs:
	tail -f $(HOME)/.local/share/fcp-mcp-lite/calls.jsonl

ui: # transcript editor (see extensions/transcript-ui/README.md)
	$(VENV)/bin/python extensions/transcript-ui/ui_server.py --port $(UI_PORT)
