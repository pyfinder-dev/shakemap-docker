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
# Override the container name:
#   make configure CONTAINER=myshakemap
#   make verify CONTAINER=myshakemap EXPECT=not-ready

CONTAINER ?= shakemap
EXPECT ?= ready
SCRIPTS := scripts

.PHONY: build start configure verify ci

build:
	$(SCRIPTS)/build-shakemap-docker.sh

start:
	$(SCRIPTS)/start-shakemap-docker.sh

configure:
	docker exec $(CONTAINER) /app/scripts/configure-shakemap.sh

verify:
	$(SCRIPTS)/verify-shakemap-deployment.sh $(CONTAINER) --expect $(EXPECT)

ci:
	$(SCRIPTS)/run-shakemap-ci-tests.sh
