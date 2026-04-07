.PHONY: dev install test clean

GO_BUILD_TAGS := exclude_graphdriver_btrfs containers_image_openpgp

# Development install: Rust + Go + editable Python
dev:
	maturin develop --release
	cd go && CGO_ENABLED=1 go build -tags "$(GO_BUILD_TAGS)" -o nitrobox-core ./cmd/nitrobox-core/

# Run tests
test:
	pytest tests/ --ignore=tests/test_checkpoint.py -q

# Clean build artifacts
clean:
	rm -f go/nitrobox-core
	cargo clean
