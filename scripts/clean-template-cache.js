#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, '..');
const templatesDir = path.join(repoRoot, 'templates');

function removeCaches(dir) {
  if (!fs.existsSync(dir)) return;
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const target = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__pycache__') {
        fs.rmSync(target, { recursive: true, force: true });
        continue;
      }
      removeCaches(target);
      continue;
    }
    if (entry.isFile() && entry.name.endsWith('.pyc')) {
      fs.rmSync(target, { force: true });
    }
  }
}

removeCaches(templatesDir);
