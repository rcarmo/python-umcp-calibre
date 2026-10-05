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
PROFILE_ROOT := evidence/test-profiles
PROFILE_DIR := $(PROFILE_ROOT)/$(RUN_ID)

export PROJECT_TMP_ROOT CACHE_ROOT BUILD_ROOT TEST_ROOT LOG_ROOT RUN_ROOT PROFILE_DIR
export TMPDIR := $(RUN_DIR)/tmp
export TMP := $(TMPDIR)
export TEMP := $(TMPDIR)
export PYTHONPYCACHEPREFIX := $(CACHE_ROOT)/python/pycache
export PIP_CACHE_DIR := $(CACHE_ROOT)/pip

.PHONY: paths init test build clean

paths:
	@scripts/project-tmp.sh paths

init:
	@scripts/project-tmp.sh init
	@mkdir -p "$(CACHE_ROOT)/python" "$(CACHE_ROOT)/pip"

test: init
	@mkdir -p "$(TMPDIR)" "$(PROFILE_ROOT)"
	PYTHONPATH=.:src python3 -W error::ResourceWarning scripts/test-profile.py
	@printf '%s\n' "Retained CPU/allocation evidence: $(PROFILE_DIR)"

build: init
	OUT="$(BUILD_ROOT)/calibre-umcp-plugin.zip" sh plugins/build-plugin.sh

clean:
	@set -eu; root="$$(PROJECT_TMP_ROOT="$(PROJECT_TMP_ROOT)" scripts/project-tmp.sh root)"; \
		test "$$root" = "$(PROJECT_TMP_ROOT)"; test ! -L "$$root"; \
		rm -rf "$$root/cache" "$$root/build" "$$root/tests" "$$root/logs" "$$root/runs"
