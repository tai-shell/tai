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

.PHONY: cpython cpython-clean clean help

help:
	@echo "Embedded-holo build targets:"
	@echo "  cpython         Configure + build + install static libpython3.13.a"
	@echo "  cpython-clean   Remove the CPython build directory only"
	@echo "  clean           Remove everything under build/"
	@echo ""
	@echo "Flags:"
	@echo "  CPYTHON_OPT=1   Enable PGO + LTO (slow; use for release builds)"

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

clean:
	rm -rf $(BUILD_DIR)
