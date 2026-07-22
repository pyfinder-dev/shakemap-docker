# ShakeMap Docker -- Convenience Makefile
#
# Scripts remain the source of truth.  This Makefile only provides
# short aliases for the common workflow commands.
#
# Workflow:
#   make build      Build the Docker image
#   make start      Start the service container
#   make configure  Configure ShakeMap inside the running container
#   make verify     Verify the running deployment
#   make ci         Run the full CI test suite
#
# Override operator resources when needed:
#   make start CONTAINER=shakemap-docker-test IMAGE=shakemap-docker:test PORT=19010
#   make verify CONTAINER=shakemap-docker-test EXPECT=not-ready

IMAGE ?= shakemap-docker:latest
CONTAINER ?= shakemap-docker
RUNTIME ?= ./runtime
PORT ?= 9010
EXPECT ?= not-ready
SCRIPTS := scripts

.PHONY: build start configure verify inspect events ci

build:
	$(SCRIPTS)/build-shakemap-docker.sh --tag $(IMAGE)

start:
	$(SCRIPTS)/start-shakemap-docker.sh --name $(CONTAINER) --runtime $(RUNTIME) --port $(PORT) --image $(IMAGE)

configure:
	docker exec $(CONTAINER) /app/scripts/configure-shakemap.sh

verify:
	$(SCRIPTS)/verify-shakemap-deployment.sh $(CONTAINER) --expect $(EXPECT)

inspect:
	docker exec $(CONTAINER) /app/scripts/inspect-shakemap-config.sh

events:
	docker exec $(CONTAINER) /app/scripts/inspect-shakemap-events.sh

ci:
	$(SCRIPTS)/run-shakemap-ci-tests.sh
