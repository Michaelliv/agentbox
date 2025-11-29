.PHONY: build dev test test-ts test-integration clean proto

# Build the sandbox image
build-sandbox:
	docker build -t agentbox-sandbox:latest ./sandbox

# Build everything
build: build-sandbox build-ts

# Run gRPC server locally (without egress proxy - full network access)
dev: build-sandbox
	PYTHONPATH=.:gen/python STORAGE_PATH=./data/tenants uv run python agentbox/grpc_server.py

# Run gRPC server with egress proxy
dev-proxy: build-sandbox
	@echo "Starting egress proxy..."
	@PYTHONPATH=. SIGNING_KEY=dev-secret uv run python agentbox/egress_proxy.py --port 15004 --signing-key dev-secret &
	@sleep 1
	@echo "Starting gRPC server..."
	PYTHONPATH=.:gen/python STORAGE_PATH=./data/tenants PROXY_HOST=host.docker.internal PROXY_PORT=15004 SIGNING_KEY=dev-secret uv run python agentbox/grpc_server.py

# Run just the egress proxy
proxy:
	PYTHONPATH=. uv run python agentbox/egress_proxy.py --port 15004 --signing-key dev-secret

# Generate protobuf code
proto:
	buf generate
	touch gen/python/__init__.py gen/python/sandbox/__init__.py gen/python/sandbox/v1/__init__.py
	cp -r gen/ts/* ts-client/src/gen/

# Build TypeScript client
build-ts:
	cd ts-client && npm install && npm run build

# Run Python example
test:
	PYTHONPATH=gen/python uv run python scripts/grpc_client_example.py

# Run TypeScript example
test-ts:
	cd ts-client && npx tsx scripts/grpc_client_example.ts

# Run integration tests (requires server running)
test-integration:
	PYTHONPATH=gen/python uv run python -m pytest tests/test_integration.py -v

# Clean up containers and images
clean:
	docker rmi agentbox-sandbox:latest 2>/dev/null || true
	docker container prune -f --filter "label=agentbox=true"
	rm -rf gen/ ts-client/dist/ ts-client/node_modules/

# Install gVisor (Linux only)
install-gvisor:
	chmod +x scripts/setup-gvisor.sh
	sudo scripts/setup-gvisor.sh

# Sync dependencies
sync:
	uv sync
