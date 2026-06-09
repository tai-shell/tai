# Embedded-holo build orchestration.
#
# Builds the pieces that get linked into tai when invoked as `tai`/`tcsh`:
# a statically-linked CPython, frozen Python sources (later), pyobjc native
# payload (later). For the design see docs/embedded-holo.md.

TAI_ROOT      := $(abspath $(CURDIR)/..)
BUILD_DIR     := $(TAI_ROOT)/build
CPYTHON_SRC   := $(TAI_ROOT)/third_party/cpython
CPYTHON_BUILD := $(BUILD_DIR)/cpython-build
CPYTHON_PFX   := $(BUILD_DIR)/cpython-install
SITE_PKGS     := $(BUILD_DIR)/site-packages
SIKULI_DIR    := $(BUILD_DIR)/sikuli

# SikuliX jar — pinned from vendor/holo/src/holo/bridge.py. Bump in
# lockstep with the vendored holo subtree pin in vendor/holo. boot.c
# points HOLO_SIKULI_JAR at the cached file at runtime so the bridge
# subprocess can locate it without an env-var dance from the user.
SIKULI_JAR_NAME := sikulixide-2.0.5.jar
SIKULI_JAR_URL  := https://github.com/nospaceleftondevice/holo/releases/download/vendored-sikulix-2.0.5/$(SIKULI_JAR_NAME)
SIKULI_JAR_SHA  := f4b0b50c8e413094e78cd1d8fed02ae65f62f8c53ed00da0562fdedf4acff729
SIKULI_JAR      := $(SIKULI_DIR)/$(SIKULI_JAR_NAME)

# Python 3.13 used for `pip install --target` into SITE_PKGS. Must be
# 3.13.x to produce wheels with the same ABI as our embedded CPython.
# Override with `make HOST_PYTHON=/path/to/python3.13` if the autoprobe
# picks something wrong.
HOST_PYTHON ?= $(shell command -v python3.13 2>/dev/null || command -v python3 2>/dev/null)

# What we need out of CPython: the static archive + the headers.
CPYTHON_LIB    := $(CPYTHON_PFX)/lib/libpython3.13.a
CPYTHON_HDR    := $(CPYTHON_PFX)/include/python3.13/Python.h
CPYTHON_CONFIG := $(CPYTHON_PFX)/bin/python3.13-config

# Configure flags. --disable-shared makes libpython3.13.a (not .dylib).
# --enable-optimizations runs PGO and adds ~30 min to the build, so it's
# off by default for dev; turn on with `make CPYTHON_OPT=1` for release.
CPYTHON_CONFIGURE_FLAGS := \
    --prefix=$(CPYTHON_PFX) \
    --disable-shared \
    --disable-test-modules \
    --with-ensurepip=no
ifeq ($(CPYTHON_OPT),1)
CPYTHON_CONFIGURE_FLAGS += --enable-optimizations --with-lto
endif

# Job count for the CPython build.
NPROC := $(shell sysctl -n hw.ncpu 2>/dev/null || echo 4)

.PHONY: cpython cpython-clean deps deps-clean sikuli-jar sikuli-clean \
        bundle bundle-clean clean help

help:
	@echo "Embedded-holo build targets:"
	@echo "  cpython         Configure + build + install static libpython3.13.a"
	@echo "  cpython-clean   Remove the CPython build directory only"
	@echo "  deps            Install holo's pure-Python + Rust-extension deps"
	@echo "                  into build/site-packages/ for embedding"
	@echo "  deps-clean      Remove build/site-packages/ only"
	@echo "  sikuli-jar      Download SikuliX jar (~123 MB) into build/sikuli/"
	@echo "                  (needed for screen_* tools; one-time)"
	@echo "  sikuli-clean    Remove build/sikuli/ only"
	@echo "  bundle          Produce build/tai-bundled — the single-file"
	@echo "                  shippable binary with payload appended"
	@echo "  bundle-clean    Remove build/bundle/ + build/tai-bundled"
	@echo "  clean           Remove everything under build/"
	@echo ""
	@echo "Flags:"
	@echo "  CPYTHON_OPT=1   Enable PGO + LTO (slow; use for release builds)"
	@echo "  HOST_PYTHON=X   Python 3.13 used to populate site-packages"
	@echo "                  (current: $(HOST_PYTHON))"

cpython: $(CPYTHON_LIB)

$(CPYTHON_LIB): $(CPYTHON_SRC)/configure
	@mkdir -p $(CPYTHON_BUILD)
	@cd $(CPYTHON_BUILD) && \
	  if [ ! -f Makefile ] || [ $(CPYTHON_SRC)/configure -nt Makefile ]; then \
	    echo "==> Configuring CPython"; \
	    $(CPYTHON_SRC)/configure $(CPYTHON_CONFIGURE_FLAGS); \
	  fi && \
	  echo "==> Building CPython (-j$(NPROC))" && \
	  $(MAKE) -j$(NPROC) && \
	  echo "==> Installing to $(CPYTHON_PFX)" && \
	  $(MAKE) install
	@test -f $(CPYTHON_LIB) || { \
	    echo "ERROR: CPython build did not produce $(CPYTHON_LIB)"; \
	    echo "Check $(CPYTHON_BUILD)/Makefile for what got built."; \
	    exit 1; \
	}
	@echo "==> libpython3.13.a OK: $$(du -h $(CPYTHON_LIB) | cut -f1)"

# The configure script ships in the source tree but the submodule's tagged
# checkouts of 3.13.x do include a pre-generated configure, so we don't
# need autoreconf. Guard against an unexpected absence anyway.
$(CPYTHON_SRC)/configure:
	@if [ ! -f $@ ]; then \
	    echo "ERROR: $@ not found"; \
	    echo "Run: git submodule update --init third_party/cpython"; \
	    exit 1; \
	fi

cpython-clean:
	rm -rf $(CPYTHON_BUILD) $(CPYTHON_PFX)

# What the embedded interpreter needs to import the holo MCP server.
# `mcp` brings in the FastMCP framework + its transitive closure
# (pydantic, anyio, httpx, …). pyobjc-framework-* powers the
# AppleScript app-activation path and holo._macos pixel-capture
# helpers. SikuliX itself ships as a jar (see `sikuli-jar` target),
# not as Python.
EMBED_DEPS = \
    mcp \
    websockets \
    pyperclip \
    pyobjc-framework-Quartz \
    pyobjc-framework-Vision \
    pyobjc-framework-Cocoa

deps: $(SITE_PKGS)/.stamp

$(SITE_PKGS)/.stamp:
	@if [ -z "$(HOST_PYTHON)" ] || ! $(HOST_PYTHON) --version 2>&1 | grep -q 'Python 3\.13\.'; then \
	    echo "ERROR: HOST_PYTHON must be Python 3.13.x"; \
	    echo "  current: $(HOST_PYTHON) ($$($(HOST_PYTHON) --version 2>&1 || echo 'not runnable'))"; \
	    echo "  override with: make deps HOST_PYTHON=/path/to/python3.13"; \
	    exit 1; \
	fi
	@mkdir -p $(SITE_PKGS)
	@echo "==> Installing $(EMBED_DEPS) into $(SITE_PKGS)"
	$(HOST_PYTHON) -m pip install --target $(SITE_PKGS) --upgrade $(EMBED_DEPS)
	@touch $@
	@echo "==> $$(du -sh $(SITE_PKGS) | cut -f1) installed"

deps-clean:
	rm -rf $(SITE_PKGS)

# SikuliX jar — large (123 MB), versioned, sha256-verified. Downloaded
# once into build/sikuli/ and reused for every screen tool call. boot.c
# sets HOLO_SIKULI_JAR=$(SIKULI_JAR) so the bridge subprocess locates it
# without user intervention.
sikuli-jar: $(SIKULI_JAR)

$(SIKULI_JAR):
	@mkdir -p $(SIKULI_DIR)
	@if command -v curl >/dev/null 2>&1; then \
	    echo "==> Downloading $(SIKULI_JAR_NAME) (~123 MB)"; \
	    curl -fL --progress-bar -o $@.partial $(SIKULI_JAR_URL); \
	else \
	    echo "ERROR: curl not found"; exit 1; \
	fi
	@echo "==> Verifying SHA-256"
	@actual=$$(shasum -a 256 $@.partial | cut -d' ' -f1); \
	if [ "$$actual" != "$(SIKULI_JAR_SHA)" ]; then \
	    echo "ERROR: sha256 mismatch on $(SIKULI_JAR_NAME)"; \
	    echo "  expected: $(SIKULI_JAR_SHA)"; \
	    echo "  got:      $$actual"; \
	    rm -f $@.partial; exit 1; \
	fi
	@mv $@.partial $@
	@echo "==> $@ ($$(du -h $@ | cut -f1))"

sikuli-clean:
	rm -rf $(SIKULI_DIR)

# ----- Bundled binary --------------------------------------------------------
#
# `bundle' builds a relocatable single-file artifact at build/tai-bundled
# by appending a payload tarball + 24-byte trailer to a copy of the
# top-level tai binary (the one that `make' produces at $(TAI_ROOT)/bash).
#
# The bundle target deliberately does NOT re-build $(TAI_ROOT)/bash —
# the top-level Makefile owns that and bash's autoconf-driven build is
# not something we want to invoke recursively here. Build order:
#   1. `make -C embedded cpython deps sikuli-jar`     (one-time)
#   2. `make` at the repo root                         (per source edit)
#   3. `make -C embedded bundle`                       (when shipping)
TAI_INPUT_BIN  := $(TAI_ROOT)/bash
TAI_OUTPUT_BIN := $(BUILD_DIR)/tai-bundled
BUNDLE_STAGE   := $(BUILD_DIR)/bundle

# Files that, when changed, force a re-bundle. Doesn't need to be
# perfect — bundle re-runs are cheap.
BUNDLE_DEPS = \
    $(TAI_INPUT_BIN) \
    $(SITE_PKGS)/.stamp \
    $(SIKULI_JAR) \
    $(wildcard $(TAI_ROOT)/embedded/runtime/tai_runtime/*.py) \
    $(wildcard $(TAI_ROOT)/vendor/holo/src/holo/*.py) \
    $(wildcard $(TAI_ROOT)/vendor/holo/bridge/*.py)

bundle: $(TAI_OUTPUT_BIN)

$(TAI_OUTPUT_BIN): $(BUNDLE_DEPS) $(TAI_ROOT)/embedded/mkbundle.sh
	@if [ ! -f $(TAI_INPUT_BIN) ]; then \
	    echo "ERROR: $(TAI_INPUT_BIN) doesn't exist"; \
	    echo "  run \`make\` at the repo root first"; \
	    exit 1; \
	fi
	@echo "==> Staging payload tree at $(BUNDLE_STAGE)"
	@rm -rf $(BUNDLE_STAGE)
	@mkdir -p $(BUNDLE_STAGE)
	@printf '1\n' > $(BUNDLE_STAGE)/version
	@cp -R $(TAI_ROOT)/embedded/runtime $(BUNDLE_STAGE)/runtime
	@mkdir -p $(BUNDLE_STAGE)/holo
	@cp -R $(TAI_ROOT)/vendor/holo/src $(BUNDLE_STAGE)/holo/src
	@cp -R $(TAI_ROOT)/vendor/holo/bridge $(BUNDLE_STAGE)/holo/bridge
	@cp -R $(SITE_PKGS) $(BUNDLE_STAGE)/site-packages
	@mkdir -p $(BUNDLE_STAGE)/sikuli
	@cp $(SIKULI_JAR) $(BUNDLE_STAGE)/sikuli/
	@find $(BUNDLE_STAGE) -name __pycache__ -type d -exec rm -rf {} +
	@find $(BUNDLE_STAGE) -name '*.pyc' -delete
	$(TAI_ROOT)/embedded/mkbundle.sh \
	    $(TAI_INPUT_BIN) $(TAI_OUTPUT_BIN) $(BUNDLE_STAGE)

bundle-clean:
	rm -rf $(BUNDLE_STAGE) $(TAI_OUTPUT_BIN)

clean:
	rm -rf $(BUILD_DIR)
