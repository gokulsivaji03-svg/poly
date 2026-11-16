import { spawn } from 'node:child_process';
import { copyFile, constants } from 'node:fs/promises';
import { createServer } from 'node:net';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const backend = fileURLToPath(new URL('../backend/', import.meta.url));
const frontend = fileURLToPath(new URL('../frontend/', import.meta.url));
const children = new Set();
let stopping = false;

function stop(code) {
  if (stopping) return;
  stopping = true;
  process.exitCode = code;
  for (const child of children) {
    if (!child.pid) continue;
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F']);
      } else {
        process.kill(-child.pid, 'SIGTERM');
      }
    } catch (error) {
      if (error.code !== 'ESRCH') console.error(error.message);
    }
  }
}

process.on('SIGINT', () => stop(0));
process.on('SIGTERM', () => stop(0));

function launch(command, args, cwd) {
  if (stopping) throw new Error('Startup interrupted');
  const child = spawn(command, args, {
    cwd, stdio: 'inherit', detached: process.platform !== 'win32',
    env: process.env,
  });
  children.add(child);
  child.once('close', () => children.delete(child));
  return child;
}

function run(command, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = launch(command, args, cwd);
    child.once('error', reject);
    child.once('close', (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} ${args.join(' ')} failed (${signal ?? code})`));
    });
  });
}

async function requireFreePort(port) {
  await new Promise((resolve, reject) => {
    const server = createServer();
    server.once('error', () => reject(new Error(
      `Port ${port} is occupied. Stop the existing server before running dev:all.`,
    )));
    server.listen(port, '127.0.0.1', () => server.close(resolve));
  });
}

try {
  await Promise.all([requireFreePort(8000), requireFreePort(5173)]);
  await run('uv', ['--version'], root);
  // Never overwrite credentials or a customized database connection.
  try {
    await copyFile(`${backend}.env.example`, `${backend}.env`, constants.COPYFILE_EXCL);
    console.log('Created backend/.env with local development defaults.');
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  }
  await run('uv', ['sync', '--frozen', '--all-groups'], backend);
  await run(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['ci', '--no-audit', '--no-fund'], frontend);
  console.log('Applying migrations (PostgreSQL must be running and the database must exist)…');
  await run('uv', ['run', 'alembic', 'upgrade', 'head'], backend);
  for (const [command, args, cwd] of [
    ['uv', ['run', 'uvicorn', 'app.main:app', '--reload', '--host', '127.0.0.1', '--port', '8000'], backend],
    [process.platform === 'win32' ? 'npm.cmd' : 'npm', ['run', 'dev', '--', '--host', '127.0.0.1', '--port', '5173', '--strictPort'], frontend],
  ]) {
    const child = launch(command, args, cwd);
    child.once('error', error => { console.error(error.message); stop(1); });
    child.once('close', (code, signal) => {
      if (!stopping) {
        console.error(`${command} exited (${signal ?? code}); stopping both servers.`);
        stop(code || 1);
      }
    });
  }
  console.log('\nTerminal: http://127.0.0.1:5173\nAPI: http://127.0.0.1:8000/api/health\nCtrl+C stops both servers.\n');
} catch (error) {
  if (!stopping) console.error(`\nStartup failed: ${error.message}`);
  stop(1);
}
