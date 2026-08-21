import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import internalServer from '../internal-mcp-server.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'm3-02a-readiness-'));
const dbPath = join(tempRoot, 'todos.db');
const readinessPath = join(root, 'scripts/check-internal-mcp-readiness.mjs');

function python(code, args = []) {
  return execFileSync(process.env.PYTHON || 'python3', ['-c', code, ...args], {
    cwd: root,
    env: { ...process.env, UH_A0_REPO_ROOT: root },
    encoding: 'utf8',
  }).trim();
}

function seedDb() {
  python(
    [
      'import sqlite3, sys',
      'conn = sqlite3.connect(sys.argv[1])',
      'conn.execute("CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, done INTEGER DEFAULT 0, due_date TEXT, author TEXT, created_at TEXT)")',
      'conn.execute("INSERT INTO todos (content, done, author, created_at) VALUES (?,?,?,?)", ("只读 readiness",0,"test","2026-08-21"))',
      'conn.commit()',
      'conn.close()',
    ].join('; '),
    [dbPath],
  );
}

function countRows() {
  return Number(python(
    'import sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); print(conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]); conn.close()',
    [dbPath],
  ));
}

function runReadiness(env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [readinessPath], {
      cwd: root,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) {
        resolve(stdout);
        return;
      }
      reject(new Error(stderr || `readiness exited with code ${code}`));
    });
  });
}

seedDb();
const before = countRows();
const { listener, port } = await internalServer.startInternalMcpServer({
  port: 0,
  dbPath,
  cwd: root,
  python: process.env.PYTHON || 'python3',
});
try {
  const source = readFileSync(readinessPath, 'utf8');
  assert.match(source, /X-UH-A0-Profile/);
  assert.doesNotMatch(source, /callTool\s*\(/);

  const output = await runReadiness({
    ...process.env,
    INTERNAL_MCP_URL: `http://127.0.0.1:${port}/mcp`,
  });
  const result = JSON.parse(output);
  assert.equal(result.status, 'PASS');
  assert.ok(['add_todo', 'get_todos'].every((name) => result.tools.includes(name)));
  assert.equal(result.write_tools_called, false);
  assert.equal(countRows(), before);
} finally {
  await new Promise((resolve) => listener.close(resolve));
  rmSync(tempRoot, { recursive: true, force: true });
}
console.log('test-m3-02a-internal-mcp-readiness: ok');
