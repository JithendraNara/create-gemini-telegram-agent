#!/usr/bin/env node

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, '..');
const templateDir = path.join(repoRoot, 'templates', 'project');

function printUsage() {
  console.log(`create-gemini-telegram-agent

Usage:
  create-gemini-telegram-agent init [--dir <path>] --bot-token <token> [options]
  create-gemini-telegram-agent doctor [--dir <path>]
  create-gemini-telegram-agent start [--dir <path>]
  create-gemini-telegram-agent sync-commands [--service-name <name>]
  create-gemini-telegram-agent uninstall [--service-name <name>] [--purge] [--dir <path>] [--yes]

Options:
  --dir <path>           Target project directory (default: ./gemini-telegram-agent-bot)
  --bot-token <token>    Telegram bot token from BotFather (required for init)
  --chat-id <id>         Optional Telegram chat allowlist id (single id)
  --model <name>         Gemini model for bot runtime (default: gemini-2.5-flash)
  --gemini-path <path>   Gemini CLI binary path (default: gemini)
  --service-name <name>  systemd user service name (default: telegram-gemini-bot)
  --no-install           Skip Python venv + pip install during init
  --no-systemd           Skip systemd unit registration during init
  --yes                  Non-interactive for destructive actions
`);
}

function fail(message, code = 1) {
  console.error(`ERROR: ${message}`);
  process.exit(code);
}

function info(message) {
  console.log(`INFO: ${message}`);
}

function warn(message) {
  console.warn(`WARN: ${message}`);
}

function parseArgs(argv) {
  const args = [...argv];
  let command = 'init';
  if (args.length > 0 && !args[0].startsWith('-')) {
    command = args.shift();
  }

  const out = {
    command,
    dir: null,
    botToken: null,
    chatId: null,
    model: 'gemini-2.5-flash',
    geminiPath: 'gemini',
    serviceName: 'telegram-gemini-bot',
    noInstall: false,
    noSystemd: false,
    purge: false,
    yes: false,
  };

  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    const next = args[i + 1];
    if (arg === '--dir') {
      if (!next) fail('--dir requires a value');
      out.dir = next;
      i += 1;
      continue;
    }
    if (arg === '--bot-token') {
      if (!next) fail('--bot-token requires a value');
      out.botToken = next;
      i += 1;
      continue;
    }
    if (arg === '--chat-id') {
      if (!next) fail('--chat-id requires a value');
      out.chatId = next;
      i += 1;
      continue;
    }
    if (arg === '--model') {
      if (!next) fail('--model requires a value');
      out.model = next;
      i += 1;
      continue;
    }
    if (arg === '--gemini-path') {
      if (!next) fail('--gemini-path requires a value');
      out.geminiPath = next;
      i += 1;
      continue;
    }
    if (arg === '--service-name') {
      if (!next) fail('--service-name requires a value');
      out.serviceName = next;
      i += 1;
      continue;
    }
    if (arg === '--no-install') {
      out.noInstall = true;
      continue;
    }
    if (arg === '--no-systemd') {
      out.noSystemd = true;
      continue;
    }
    if (arg === '--purge') {
      out.purge = true;
      continue;
    }
    if (arg === '--yes') {
      out.yes = true;
      continue;
    }
    if (arg === '-h' || arg === '--help') {
      out.command = 'help';
      continue;
    }
    if (!arg.startsWith('-') && !out.dir) {
      out.dir = arg;
      continue;
    }
    fail(`Unknown argument: ${arg}`);
  }

  return out;
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    stdio: options.stdio ?? 'pipe',
    cwd: options.cwd,
    env: options.env,
    encoding: 'utf-8',
  });

  if (result.error) {
    return {
      ok: false,
      code: 1,
      stdout: result.stdout || '',
      stderr: result.error.message || result.stderr || '',
    };
  }

  return {
    ok: result.status === 0,
    code: result.status ?? 1,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
  };
}

function commandExists(name) {
  const result = run('bash', ['-lc', `command -v ${name}`]);
  return result.ok;
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function isDirectoryEmpty(dirPath) {
  if (!fs.existsSync(dirPath)) return true;
  return fs.readdirSync(dirPath).length === 0;
}

function copyDir(source, target) {
  ensureDir(target);
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const srcPath = path.join(source, entry.name);
    const dstPath = path.join(target, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, dstPath);
      continue;
    }
    if (entry.isSymbolicLink()) {
      const linkTarget = fs.readlinkSync(srcPath);
      fs.symlinkSync(linkTarget, dstPath);
      continue;
    }
    fs.copyFileSync(srcPath, dstPath);
  }
}

function writeFile(filePath, content, mode = 0o644) {
  ensureDir(path.dirname(filePath));
  fs.writeFileSync(filePath, content, { mode });
  fs.chmodSync(filePath, mode);
}

function renderTemplate(content, vars) {
  let rendered = content;
  for (const [key, value] of Object.entries(vars)) {
    rendered = rendered.replaceAll(`{{${key}}}`, value);
  }
  return rendered;
}

function loadEnvFile(envPath) {
  const out = {};
  if (!fs.existsSync(envPath)) return out;
  const raw = fs.readFileSync(envPath, 'utf-8');
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const idx = trimmed.indexOf('=');
    if (idx <= 0) continue;
    const key = trimmed.slice(0, idx).trim();
    const value = trimmed.slice(idx + 1).trim();
    out[key] = value;
  }
  return out;
}

function buildRuntimeEnv(projectDir) {
  const envPath = path.join(projectDir, '.env');
  const envFromFile = loadEnvFile(envPath);
  const merged = { ...process.env };
  for (const [key, value] of Object.entries(envFromFile)) {
    merged[key] = value;
  }
  return merged;
}

function installPythonDeps(projectDir) {
  const python = commandExists('python3') ? 'python3' : null;
  if (!python) {
    warn('python3 not found; skipping venv setup.');
    return false;
  }

  const venvPath = path.join(projectDir, '.venv');
  if (!fs.existsSync(venvPath)) {
    info('Creating Python virtual environment...');
    const mkVenv = run(python, ['-m', 'venv', '.venv'], { cwd: projectDir });
    if (!mkVenv.ok) {
      warn(`venv creation failed: ${mkVenv.stderr.trim() || mkVenv.stdout.trim()}`);
      return false;
    }
  }

  const pipPath = path.join(projectDir, '.venv', 'bin', 'pip');
  if (!fs.existsSync(pipPath)) {
    warn(`pip not found at ${pipPath}; skipping dependency install.`);
    return false;
  }

  info('Installing Python dependencies...');
  const pipInstall = run(pipPath, ['install', '-r', 'requirements.txt'], {
    cwd: projectDir,
    stdio: 'inherit',
  });
  if (!pipInstall.ok) {
    warn('Dependency installation failed; run manually later.');
    return false;
  }
  return true;
}

function installSystemdUnit(projectDir, serviceName) {
  const unitTemplatePath = path.join(projectDir, 'telegram-gemini-bot.service.tmpl');
  if (!fs.existsSync(unitTemplatePath)) {
    warn('Service template missing; skipping systemd registration.');
    return false;
  }

  const userUnitDir = path.join(os.homedir(), '.config', 'systemd', 'user');
  ensureDir(userUnitDir);
  const unitPath = path.join(userUnitDir, `${serviceName}.service`);

  const unitContent = fs.readFileSync(unitTemplatePath, 'utf-8');
  const rendered = renderTemplate(unitContent, {
    PROJECT_DIR: projectDir,
    SERVICE_NAME: serviceName,
  });
  writeFile(unitPath, rendered, 0o644);

  if (!commandExists('systemctl')) {
    warn('systemctl not found; unit file written but not enabled.');
    return false;
  }

  const daemonReload = run('systemctl', ['--user', 'daemon-reload']);
  if (!daemonReload.ok) {
    warn(`systemd daemon-reload failed: ${daemonReload.stderr.trim()}`);
    return false;
  }

  const enableNow = run('systemctl', ['--user', 'enable', '--now', `${serviceName}.service`]);
  if (!enableNow.ok) {
    warn(`Failed to enable/start service: ${enableNow.stderr.trim()}`);
    return false;
  }

  info(`Service enabled: ${serviceName}.service`);
  return true;
}

function checkCommand(name, args = ['--version']) {
  const result = run(name, args);
  return {
    ok: result.ok,
    detail: result.ok
      ? (result.stdout.trim() || result.stderr.trim() || 'ok')
      : (result.stderr.trim() || result.stdout.trim() || 'not available'),
  };
}

function commandInit(options) {
  if (!options.botToken) {
    fail('init requires --bot-token <token>');
  }

  const projectDir = path.resolve(options.dir || 'gemini-telegram-agent-bot');
  if (!isDirectoryEmpty(projectDir)) {
    fail(`Target directory is not empty: ${projectDir}`);
  }

  info(`Scaffolding project at ${projectDir}`);
  copyDir(templateDir, projectDir);

  const tokenDir = path.join(os.homedir(), '.config', 'gemini-telegram-agent');
  const tokenPath = path.join(tokenDir, 'telegram-bot-token.txt');
  ensureDir(tokenDir);
  writeFile(tokenPath, `${options.botToken.trim()}\n`, 0o600);

  const envContent = [
    '# Runtime configuration for Gemini Telegram Agent',
    `BOT_TOKEN_FILE=${tokenPath}`,
    `GEMINI_PATH=${options.geminiPath}`,
    `GEMINI_MODEL=${options.model}`,
    'REQUEST_TIMEOUT_SEC=300',
    'MAX_CONCURRENT_PROCESSES=3',
    'MAX_INPUT_CHARS=2000',
    'RATE_LIMIT_PER_MIN=12',
    'RATE_LIMIT_BURST=3',
    'RATE_LIMIT_WINDOW_SEC=60',
    'CLI_TIMEOUT_SEC=60',
    'CLI_CHAIN_MAX=8',
    'SESSIONS_LIST_DEFAULT_LIMIT=25',
    'SESSIONS_LIST_MAX_LIMIT=200',
    'SESSION_IDLE_RESET_SEC=1800',
    options.chatId ? `ALLOWED_CHAT_IDS=${options.chatId}` : 'ALLOWED_CHAT_IDS=',
    '',
  ].join('\n');
  writeFile(path.join(projectDir, '.env'), envContent, 0o600);

  if (!options.noInstall) {
    installPythonDeps(projectDir);
  }

  let systemdInstalled = false;
  if (!options.noSystemd) {
    systemdInstalled = installSystemdUnit(projectDir, options.serviceName);
  }

  info('Scaffold complete.');
  console.log('');
  let step = 1;
  console.log('Next steps:');
  console.log(`${step}. cd ${projectDir}`);
  step += 1;
  console.log(`${step}. Verify Gemini CLI auth: gemini --list-sessions`);
  step += 1;
  if (options.noInstall) {
    console.log(`${step}. python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`);
    step += 1;
  }
  if (!options.noSystemd) {
    if (!systemdInstalled) {
      console.log(`${step}. systemctl --user daemon-reload && systemctl --user enable --now ${options.serviceName}.service`);
      step += 1;
    }
    console.log(`${step}. systemctl --user status ${options.serviceName}.service`);
  } else {
    console.log(`${step}. .venv/bin/python bot.py`);
  }
}

function commandDoctor(options) {
  const projectDir = path.resolve(options.dir || process.cwd());
  const env = loadEnvFile(path.join(projectDir, '.env'));

  const checks = [];
  checks.push({ name: 'Project directory', ok: fs.existsSync(projectDir), detail: projectDir });
  checks.push({
    name: 'bot.py present',
    ok: fs.existsSync(path.join(projectDir, 'bot.py')),
    detail: path.join(projectDir, 'bot.py'),
  });

  const geminiPath = env.GEMINI_PATH || options.geminiPath || 'gemini';
  checks.push({ name: 'Gemini binary', ...checkCommand(geminiPath, ['--version']) });
  checks.push({ name: 'Python', ...checkCommand('python3', ['--version']) });

  const tokenFile = env.BOT_TOKEN_FILE || path.join(os.homedir(), '.config', 'gemini-telegram-agent', 'telegram-bot-token.txt');
  const tokenExists = fs.existsSync(tokenFile);
  let tokenDetail = tokenExists ? 'present' : 'missing';
  let tokenSecure = false;
  if (tokenExists) {
    const stat = fs.statSync(tokenFile);
    tokenSecure = (stat.mode & 0o077) === 0;
    tokenDetail = tokenSecure ? 'present (mode 600)' : 'present (permissions too open)';
  }
  checks.push({ name: 'Bot token file', ok: tokenExists && tokenSecure, detail: `${tokenFile} - ${tokenDetail}` });

  const pyBin = fs.existsSync(path.join(projectDir, '.venv', 'bin', 'python'))
    ? path.join(projectDir, '.venv', 'bin', 'python')
    : 'python3';
  const pyCompile = run(pyBin, ['-m', 'py_compile', 'bot.py'], { cwd: projectDir });
  checks.push({
    name: 'Python compile',
    ok: pyCompile.ok,
    detail: pyCompile.ok ? 'ok' : (pyCompile.stderr.trim() || pyCompile.stdout.trim() || 'failed'),
  });

  const serviceName = options.serviceName;
  const hasSystemctl = commandExists('systemctl');
  const userUnitPath = path.join(os.homedir(), '.config', 'systemd', 'user', `${serviceName}.service`);
  if (hasSystemctl && fs.existsSync(userUnitPath)) {
    const svc = run('systemctl', ['--user', 'is-active', `${serviceName}.service`]);
    checks.push({
      name: `Service ${serviceName}`,
      ok: svc.ok && svc.stdout.trim() === 'active',
      detail: svc.stdout.trim() || svc.stderr.trim(),
    });
  } else {
    checks.push({
      name: `Service ${serviceName}`,
      ok: true,
      detail: hasSystemctl ? 'unit not configured (optional)' : 'systemctl not available (optional)',
    });
  }

  let failed = 0;
  console.log(`Doctor report for ${projectDir}`);
  console.log('');
  for (const check of checks) {
    const status = check.ok ? 'PASS' : 'FAIL';
    console.log(`${status.padEnd(4)}  ${check.name}: ${check.detail}`);
    if (!check.ok) failed += 1;
  }

  if (failed > 0) {
    process.exit(1);
  }
}

function commandStart(options) {
  const projectDir = path.resolve(options.dir || process.cwd());
  const runtimeEnv = buildRuntimeEnv(projectDir);
  const pyBin = fs.existsSync(path.join(projectDir, '.venv', 'bin', 'python'))
    ? path.join(projectDir, '.venv', 'bin', 'python')
    : 'python3';
  const result = spawnSync(pyBin, ['bot.py'], {
    cwd: projectDir,
    stdio: 'inherit',
    env: runtimeEnv,
  });
  process.exit(result.status ?? 1);
}

function commandSyncCommands(options) {
  if (!commandExists('systemctl')) {
    fail('systemctl is required for sync-commands.');
  }
  const serviceName = options.serviceName;
  const restart = run('systemctl', ['--user', 'restart', `${serviceName}.service`]);
  if (!restart.ok) {
    fail(`Failed to restart ${serviceName}.service: ${restart.stderr.trim() || restart.stdout.trim()}`);
  }
  info(`Restarted ${serviceName}.service; Telegram command menu will sync on startup.`);
}

function commandUninstall(options) {
  const serviceName = options.serviceName;
  const projectDir = path.resolve(options.dir || process.cwd());
  const unitPath = path.join(os.homedir(), '.config', 'systemd', 'user', `${serviceName}.service`);

  if (!options.yes) {
    fail('uninstall is destructive. Re-run with --yes.');
  }

  if (commandExists('systemctl')) {
    run('systemctl', ['--user', 'disable', '--now', `${serviceName}.service`]);
  }

  if (fs.existsSync(unitPath)) {
    fs.rmSync(unitPath);
    info(`Removed ${unitPath}`);
  }

  if (commandExists('systemctl')) {
    run('systemctl', ['--user', 'daemon-reload']);
  }

  if (options.purge && fs.existsSync(projectDir)) {
    fs.rmSync(projectDir, { recursive: true, force: true });
    info(`Removed project directory: ${projectDir}`);
  }
}

function main() {
  const parsed = parseArgs(process.argv.slice(2));
  switch (parsed.command) {
    case 'help':
      printUsage();
      return;
    case 'init':
      commandInit(parsed);
      return;
    case 'doctor':
      commandDoctor(parsed);
      return;
    case 'start':
      commandStart(parsed);
      return;
    case 'sync-commands':
      commandSyncCommands(parsed);
      return;
    case 'uninstall':
      commandUninstall(parsed);
      return;
    default:
      fail(`Unknown command: ${parsed.command}`);
  }
}

main();
