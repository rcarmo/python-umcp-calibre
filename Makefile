SHELL := /bin/bash

PROJECT := calibre-umcp
PROJECT_TMP_ROOT := /workspace/tmp/$(PROJECT)
CACHE_ROOT := $(PROJECT_TMP_ROOT)/cache
BUILD_ROOT := $(PROJECT_TMP_ROOT)/build
RUN_ROOT := $(PROJECT_TMP_ROOT)/runs
RUN_ID ?= $(shell date -u +%Y%m%dT%H%M%S%NZ)
RUN_DIR := $(RUN_ROOT)/test/$(RUN_ID)
PROFILE_ROOT := evidence/test-profiles
PROFILE_DIR := $(PROFILE_ROOT)/$(RUN_ID)

export PROJECT_TMP_ROOT CACHE_ROOT BUILD_ROOT RUN_ROOT PROFILE_DIR
export TMPDIR := $(RUN_DIR)/tmp
export TMP := $(TMPDIR)
export TEMP := $(TMPDIR)
export PYTHONPYCACHEPREFIX := $(CACHE_ROOT)/python/pycache
export PIP_CACHE_DIR := $(CACHE_ROOT)/pip

.PHONY: paths init test build clean

paths:
	@printf '%s\n' "PROJECT_TMP_ROOT=$(PROJECT_TMP_ROOT)" "CACHE_ROOT=$(CACHE_ROOT)" "BUILD_ROOT=$(BUILD_ROOT)" "RUN_ROOT=$(RUN_ROOT)"

init:
	@set -eu; for path in /workspace/tmp "$(PROJECT_TMP_ROOT)" "$(CACHE_ROOT)" "$(BUILD_ROOT)" "$(RUN_ROOT)"; do \
		test ! -L "$$path" || { echo "Refusing symlink scratch path: $$path" >&2; exit 1; }; \
		if test -e "$$path"; then test -d "$$path" && test -O "$$path" || { echo "Scratch path must be an owned directory: $$path" >&2; exit 1; }; fi; \
	done; mkdir -p "$(CACHE_ROOT)/python" "$(CACHE_ROOT)/pip" "$(BUILD_ROOT)" "$(RUN_ROOT)"

test: init
	@mkdir -p "$(TMPDIR)" "$(PROFILE_ROOT)"
	PYTHONPATH=.:src python3 -W error::ResourceWarning scripts/test-profile.py
	@printf '%s\n' "Retained CPU/allocation evidence: $(PROFILE_DIR)"

build: init
	OUT="$(BUILD_ROOT)/calibre-umcp-plugin.zip" sh plugins/build-plugin.sh

clean:
	@set -eu; test "$(PROJECT_TMP_ROOT)" = /workspace/tmp/calibre-umcp; \
		test ! -L "$(PROJECT_TMP_ROOT)"; \
		rm -rf "$(CACHE_ROOT)" "$(BUILD_ROOT)" "$(RUN_ROOT)"
