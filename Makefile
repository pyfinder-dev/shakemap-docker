# Thin aliases; scripts remain the source of truth.
IMAGE ?= shakemap-docker:latest
CONTAINER ?= shakemap-docker
RUNTIME ?= ./runtime
PORT ?= 9010
SCRIPTS := scripts

.PHONY: build start verify-image verify-deployment test

build:
	$(SCRIPTS)/build-shakemap-docker.sh

start:
	$(SCRIPTS)/start-shakemap-docker.sh --name $(CONTAINER) --runtime $(RUNTIME) --port $(PORT) --image $(IMAGE)

verify-image:
	docker run --rm --network none --entrypoint /app/scripts/verify-shakemap-image.sh $(IMAGE)

verify-deployment:
	$(SCRIPTS)/verify-shakemap-deployment.sh --url http://localhost:$(PORT)

test:
	@for f in tests/test_*.py; do python $$f || exit; done
