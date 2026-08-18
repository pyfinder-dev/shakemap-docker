# Public commands are thin aliases to responsibility-specific helpers.
RUNTIME_ROOT ?= ./runtime
PORT ?= 9010
MAX_CONCURRENT ?= 10
DATA_ACTION ?= provision
SCRIPTS := scripts
# Expand the exported value inside shell quotes so spaces and shell punctuation
# remain one target argument.
PERMISSION_TARGET_OPTION = $(if $(strip $(value PERMISSION_TARGET)),--target "$${PERMISSION_TARGET_VALUE}",)

.PHONY: build data fix-permissions finalize start stop verify

build:
	$(SCRIPTS)/build-shakemap-docker.sh

data:
	$(SCRIPTS)/manage-shakemap-data.sh $(DATA_ACTION) --runtime $(RUNTIME_ROOT)

fix-permissions: export PERMISSION_TARGET_VALUE := $(value PERMISSION_TARGET)
fix-permissions:
	$(SCRIPTS)/fix-shakemap-permissions.sh --runtime-root "$(RUNTIME_ROOT)" $(PERMISSION_TARGET_OPTION)

finalize:
	$(SCRIPTS)/finalize-shakemap.sh --runtime-root $(RUNTIME_ROOT) --port $(PORT) --max-concurrent $(MAX_CONCURRENT)

start:
	$(SCRIPTS)/start-shakemap-docker.sh --runtime-root $(RUNTIME_ROOT) --port $(PORT) --max-concurrent $(MAX_CONCURRENT)

stop:
	$(SCRIPTS)/stop-shakemap-docker.sh

verify:
	$(SCRIPTS)/verify-shakemap-deployment.sh --runtime-root $(RUNTIME_ROOT) --port $(PORT) --max-concurrent $(MAX_CONCURRENT)
