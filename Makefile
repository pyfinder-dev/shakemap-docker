# Public commands are thin aliases to responsibility-specific helpers.
RUNTIME_ROOT ?= ./runtime
PORT ?= 9010
MAX_CONCURRENT ?= 10
DATA_ACTION ?= provision
SCRIPTS := scripts

.PHONY: build data finalize start stop verify

build:
	$(SCRIPTS)/build-shakemap-docker.sh

data:
	$(SCRIPTS)/manage-shakemap-data.sh $(DATA_ACTION) --runtime $(RUNTIME_ROOT)

finalize:
	$(SCRIPTS)/finalize-shakemap.sh --runtime-root $(RUNTIME_ROOT) --port $(PORT) --max-concurrent $(MAX_CONCURRENT)

start:
	$(SCRIPTS)/start-shakemap-docker.sh --runtime-root $(RUNTIME_ROOT) --port $(PORT) --max-concurrent $(MAX_CONCURRENT)

stop:
	$(SCRIPTS)/stop-shakemap-docker.sh

verify:
	$(SCRIPTS)/verify-shakemap-deployment.sh --runtime-root $(RUNTIME_ROOT) --port $(PORT) --max-concurrent $(MAX_CONCURRENT)
