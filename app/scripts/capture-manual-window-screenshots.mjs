import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(__dirname, '../../artifacts/manual-window-screenshots');
fs.mkdirSync(outDir, { recursive: true });

const preview = spawn('npm', ['run', 'preview', '--', '--host', '127.0.0.1', '--port', '4173'], {
  cwd: path.join(__dirname, '..'),
  stdio: 'pipe',
});

await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('preview timeout')), 20000);
  preview.stdout?.on('data', (buf) => {
    if (String(buf).includes('4173')) {
      clearTimeout(timer);
      resolve(undefined);
    }
  });
  preview.stderr?.on('data', (buf) => {
    if (String(buf).includes('4173')) {
      clearTimeout(timer);
      resolve(undefined);
    }
  });
});

const browser = await chromium.launch({ executablePath: '/usr/local/bin/google-chrome', args: ['--no-sandbox'] });

const shots = [
  { name: 'toolbar-390', width: 390, height: 844, url: '/dash/manual-context-window?shot=toolbar' },
  { name: 'modal0-334', width: 334, height: 800, url: '/dash/manual-context-window?shot=modal0' },
  { name: 'modal5-390', width: 390, height: 800, url: '/dash/manual-context-window?shot=modal5' },
  { name: 'busy-390', width: 390, height: 800, url: '/dash/manual-context-window?shot=busy' },
  { name: 'disabled-390', width: 390, height: 800, url: '/dash/manual-context-window?shot=disabled' },
  { name: 'desktop-1200', width: 1200, height: 900, url: '/dash/manual-context-window?shot=modal5' },
];

for (const shot of shots) {
  const page = await browser.newPage({ viewport: { width: shot.width, height: shot.height } });
  await page.goto(`http://127.0.0.1:4173${shot.url}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(outDir, `${shot.name}.png`), fullPage: false });
  await page.close();
}

await browser.close();
preview.kill('SIGTERM');
console.log(`Screenshots saved to ${outDir}`);
