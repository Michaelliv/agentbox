#!/usr/bin/env npx tsx
/**
 * Example TypeScript gRPC client for the Sandbox service.
 *
 * Usage:
 *     cd ts-client && npx tsx scripts/grpc_client_example.ts
 *
 * Requires the gRPC server to be running (make dev).
 */

import { createClient } from "@connectrpc/connect";
import { createGrpcTransport } from "@connectrpc/connect-node";
import { SandboxService } from "../src/gen/sandbox/v1/sandbox_pb.js";

async function main() {
  // Create gRPC transport for Node.js (native HTTP/2)
  const transport = createGrpcTransport({
    baseUrl: "http://localhost:50051",
    httpVersion: "2",
  });

  const client = createClient(SandboxService, transport);

  console.log("=== TypeScript gRPC Client Test ===\n");

  // Create session with default allowed hosts
  console.log("Creating session (with default allowed hosts)...");
  const createResponse = await client.createSession({});
  const session = createResponse.session!;
  const sessionId = session.sessionId;
  console.log(`  Session ID: ${sessionId}`);
  console.log(`  Container: ${session.containerId}`);
  console.log(`  Allowed hosts: ${JSON.stringify(session.allowedHosts)}`);

  try {
    // Execute command
    console.log("\nExecuting 'python3 --version'...");
    let result = await client.exec({
      sessionId,
      command: "python3 --version",
      timeout: 30,
      workdir: "/workspace",
    });
    console.log(`  Exit code: ${result.exitCode}`);
    console.log(`  Output: ${result.stdout.trim()}`);

    // Write file
    console.log("\nWriting test.py...");
    const writeResponse = await client.writeFile({
      sessionId,
      path: "test.py",
      content: "print('Hello from gRPC!')",
      mode: "w",
    });
    console.log(`  Success: ${writeResponse.success}`);

    // Read file
    console.log("\nReading test.py...");
    const readResponse = await client.readFile({
      sessionId,
      path: "test.py",
    });
    console.log(`  Content: ${readResponse.content}`);

    // Execute file
    console.log("\nExecuting test.py...");
    result = await client.exec({
      sessionId,
      command: "python3 test.py",
      timeout: 30,
      workdir: "/workspace",
    });
    console.log(`  Output: ${result.stdout.trim()}`);

    // Check kernel (should show gVisor if using runsc)
    console.log("\nChecking kernel...");
    result = await client.exec({
      sessionId,
      command: "uname -r",
      timeout: 30,
      workdir: "/workspace",
    });
    console.log(`  Kernel: ${result.stdout.trim()}`);

    // List sessions
    console.log("\nListing sessions...");
    const listResponse = await client.listSessions({});
    console.log(`  Active sessions: ${listResponse.sessions.length}`);
  } finally {
    // Cleanup
    console.log("\nDestroying session...");
    const destroyResponse = await client.destroySession({ sessionId });
    console.log(`  Success: ${destroyResponse.success}`);
  }

  console.log("\nDone!");
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
