/**
 * TypeScript SDK for agentbox
 *
 * Works in:
 * - Browser (via Connect-Web with gRPC-Web proxy)
 * - Node.js (via Connect-Node for native gRPC)
 *
 * Example:
 * ```ts
 * import { Sandbox } from '@agentbox/client';
 *
 * const sandbox = new Sandbox('http://localhost:50051');
 *
 * // Create with default allowed hosts (pypi, npm, github)
 * await sandbox.create();
 *
 * // Or specify custom allowed hosts
 * await sandbox.create({ allowedHosts: ['pypi.org', 'files.pythonhosted.org'] });
 *
 * const result = await sandbox.exec('python3 --version');
 * console.log(result.stdout);
 *
 * await sandbox.destroy();
 * ```
 */

import { createClient, type Client } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import {
  SandboxService,
  type SessionInfo,
  type ExecResponse,
  type ExecStreamResponse,
} from "./gen/sandbox/v1/sandbox_pb.js";

export interface SandboxOptions {
  sessionId?: string;
  tenantId?: string;
  /**
   * List of allowed egress hosts (e.g., "pypi.org", "github.com").
   * - If undefined or empty: uses defaults (package registries, GitHub)
   * - If specified with hosts: only those hosts are allowed
   *
   * Note: Due to proto3 semantics, empty arrays cannot be distinguished from
   * unset values, so both use defaults. To disable network access entirely,
   * use a non-routable host like ["localhost"].
   */
  allowedHosts?: string[];
}

export interface ExecOptions {
  timeout?: number;
  workdir?: string;
}

export class Sandbox {
  private client: Client<typeof SandboxService>;
  private _sessionId: string | null = null;
  private _allowedHosts: string[] = [];

  constructor(baseUrl: string = "http://localhost:50051") {
    const transport = createGrpcWebTransport({
      baseUrl,
    });
    this.client = createClient(SandboxService, transport);
  }

  /**
   * Get the current session ID
   */
  get sessionId(): string | null {
    return this._sessionId;
  }

  /**
   * Get the allowed hosts for this session
   */
  get allowedHosts(): string[] {
    return this._allowedHosts;
  }

  /**
   * Check if network is enabled for this session
   */
  get networkEnabled(): boolean {
    return this._allowedHosts.length > 0;
  }

  /**
   * Create a new sandbox session
   */
  async create(options: SandboxOptions = {}): Promise<SessionInfo> {
    const response = await this.client.createSession({
      sessionId: options.sessionId,
      tenantId: options.tenantId,
      allowedHosts: options.allowedHosts,
    });

    if (response.session) {
      this._sessionId = response.session.sessionId;
      this._allowedHosts = [...response.session.allowedHosts];
    }

    return response.session!;
  }

  /**
   * Destroy the current session
   */
  async destroy(): Promise<boolean> {
    if (!this._sessionId) return false;

    const response = await this.client.destroySession({
      sessionId: this._sessionId,
    });

    this._sessionId = null;
    this._allowedHosts = [];
    return response.success;
  }

  /**
   * Execute a command and wait for completion
   */
  async exec(command: string, options: ExecOptions = {}): Promise<ExecResponse> {
    this.ensureSession();

    return await this.client.exec({
      sessionId: this._sessionId!,
      command,
      timeout: options.timeout ?? 30,
      workdir: options.workdir ?? "/workspace",
    });
  }

  /**
   * Execute a command and stream output
   */
  async *execStream(
    command: string,
    workdir: string = "/workspace"
  ): AsyncGenerator<ExecStreamResponse> {
    this.ensureSession();

    const stream = this.client.execStream({
      sessionId: this._sessionId!,
      command,
      workdir,
    });

    for await (const chunk of stream) {
      yield chunk;
    }
  }

  /**
   * Write a file to the sandbox
   */
  async writeFile(
    path: string,
    content: string,
    mode: "w" | "a" = "w"
  ): Promise<void> {
    this.ensureSession();

    const response = await this.client.writeFile({
      sessionId: this._sessionId!,
      path,
      content,
      mode,
    });

    if (!response.success) {
      throw new Error(`Failed to write file: ${response.error}`);
    }
  }

  /**
   * Read a file from the sandbox
   */
  async readFile(path: string): Promise<string> {
    this.ensureSession();

    const response = await this.client.readFile({
      sessionId: this._sessionId!,
      path,
    });

    if (!response.success) {
      throw new Error(`Failed to read file: ${response.error}`);
    }

    return response.content ?? "";
  }

  /**
   * Install Python packages (requires pypi.org in allowed hosts)
   */
  async pipInstall(packages: string[]): Promise<ExecResponse> {
    this.ensureSession();

    const requiredHosts = ["pypi.org", "files.pythonhosted.org"];
    const missingHosts = requiredHosts.filter(
      (h) => !this._allowedHosts.includes(h)
    );

    if (missingHosts.length > 0) {
      throw new Error(
        `pip install requires ${missingHosts.join(", ")} in allowedHosts`
      );
    }

    return await this.client.pipInstall({
      sessionId: this._sessionId!,
      packages,
    });
  }

  /**
   * Run Python code
   */
  async runPython(code: string, timeout: number = 30): Promise<ExecResponse> {
    await this.writeFile("_temp_script.py", code);
    try {
      return await this.exec("python3 _temp_script.py", { timeout });
    } finally {
      // Always cleanup temp file, even on error
      await this.exec("rm -f _temp_script.py").catch(() => {
        // Ignore cleanup errors
      });
    }
  }

  private ensureSession(): void {
    if (!this._sessionId) {
      throw new Error("No active session. Call create() first.");
    }
  }
}

// Re-export generated types
export * from "./gen/sandbox/v1/sandbox_pb.js";
