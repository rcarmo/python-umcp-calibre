SHELL := /bin/bash

PROJECT := calibre-umcp
# Resolve once before TMPDIR is exported. An explicit PROJECT_TMP_ROOT is validated
# by the vendored resolver; otherwise it applies the documented portable fallback.
PROJECT_TMP_ROOT := $(shell scripts/project-tmp.sh root)
ifeq ($(strip $(PROJECT_TMP_ROOT)),)
$(error Unable to resolve PROJECT_TMP_ROOT; see resolver diagnostics above)
endif
CACHE_ROOT := $(PROJECT_TMP_ROOT)/cache
BUILD_ROOT := $(PROJECT_TMP_ROOT)/build
TEST_ROOT := $(PROJECT_TMP_ROOT)/tests
LOG_ROOT := $(PROJECT_TMP_ROOT)/logs
RUN_ROOT := $(PROJECT_TMP_ROOT)/runs
RUN_ID ?= $(shell date -u +%Y%m%dT%H%M%S%NZ)
RUN_DIR := $(TEST_ROOT)/$(RUN_ID)
PROFILE_DIR := $(RUN_ROOT)/pre-release/$(RUN_ID)/profiles

export PROJECT_TMP_ROOT CACHE_ROOT BUILD_ROOT TEST_ROOT LOG_ROOT RUN_ROOT PROFILE_DIR
export TMPDIR := $(RUN_DIR)/tmp
export TMP := $(TMPDIR)
export TEMP := $(TMPDIR)
export PYTHONPYCACHEPREFIX := $(CACHE_ROOT)/python/pycache
export PIP_CACHE_DIR := $(CACHE_ROOT)/pip

.PHONY: paths init test prerelease-test build clean

paths:
	@scripts/project-tmp.sh paths

init:
	@scripts/project-tmp.sh init
	@mkdir -p "$(CACHE_ROOT)/python" "$(CACHE_ROOT)/pip"

test: init
	@set -eu; trap 'rm -rf "$(RUN_DIR)"' EXIT; mkdir -p "$(TMPDIR)"; \
		PYTHONPATH=.:src python3 -W error::ResourceWarning -m unittest discover -v tests

prerelease-test: init
	@set -eu; trap 'rm -rf "$(RUN_DIR)" "$(RUN_ROOT)/pre-release/$(RUN_ID)"' EXIT; mkdir -p "$(TMPDIR)"; \
		PYTHONPATH=.:src python3 -W error::ResourceWarning scripts/test-profile.py

build: init
	OUT="$(BUILD_ROOT)/calibre-umcp-plugin.zip" sh plugins/build-plugin.sh

clean:
	@set -eu; root="$$(PROJECT_TMP_ROOT="$(PROJECT_TMP_ROOT)" scripts/project-tmp.sh root)"; \
		test "$$root" = "$(PROJECT_TMP_ROOT)"; test ! -L "$$root"; \
		rm -rf "$$root/cache" "$$root/build" "$$root/tests" "$$root/logs" "$$root/runs"
