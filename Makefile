VERSION := $(shell cat VERSION)
RUNTIME := $(shell command -v podman || command -v docker)

.PHONY: help lint test test-api test-e2e test-live test-smoke image package install clean

help:
	@echo "harvester-ops $(VERSION)"
	@echo "Targets:"
	@echo "  lint        Run shellcheck and py syntax checks"
	@echo "  test        Run all non-live tests (api + e2e)"
	@echo "  test-api    Run pytest API tests (Flask, no browser)"
	@echo "  test-e2e    Run pytest E2E tests (Playwright)"
	@echo "  test-live   Run all tests including those needing real harv1"
	@echo "  test-smoke  Run the minimal bash smoke test"
	@echo "  image       Build the container image"
	@echo "  package     Build the airgap tarball under dist/"
	@echo "  install     Run ./install.sh (requires root)"
	@echo "  clean       Remove dist/, web/vendor/, .pytest_cache, __pycache__"

lint:
	@bash -n bin/lib/common.sh
	@bash -n bin/harvester-shutdown.sh
	@bash -n bin/harvester-startup.sh
	@bash -n bin/harvester-status.sh
	@bash -n install.sh
	@bash -n uninstall.sh
	@bash -n package.sh
	@python3 -c "import ast; ast.parse(open('web/app.py').read())"
	@echo "OK"

test:
	@python3 -m pytest tests/api tests/e2e -v

test-api:
	@python3 -m pytest tests/api -v

test-e2e:
	@python3 -m pytest tests/e2e -v

test-live:
	@python3 -m pytest tests/ --live -v

test-smoke:
	@bash tests/smoke.sh

image:
	$(RUNTIME) build -t harvester-ops:$(VERSION) -t harvester-ops:latest \
	    -f container/Containerfile .

package:
	./package.sh

install:
	sudo ./install.sh

clean:
	rm -rf dist web/vendor images/*.tar .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
