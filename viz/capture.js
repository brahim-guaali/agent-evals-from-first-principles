// Capture frames of a replayed trace (or run) from the viewer: one screenshot per moment.
//   node viz/capture.js <url with ?replay=...> <out dir> [frames] [width] [height] [dark]
// The page must expose window.ready, window.replayEnd and window.seek(t) (services/ui/*.html).
const path = require("path");
const { chromium } = require(process.env.PLAYWRIGHT || "playwright");
(async () => {
  const [url, out, n = "60", w = "1100", h = "900", scheme = "light"] = process.argv.slice(2);
  const fs = require("fs");
  fs.mkdirSync(out, { recursive: true });
  for (const f of fs.readdirSync(out)) if (f.endsWith(".png")) fs.unlinkSync(path.join(out, f));
  const browser = await chromium.launch(process.env.CHROME_CHANNEL ? { channel: process.env.CHROME_CHANNEL } : {});
  const page = await browser.newPage({ viewport: { width: +w, height: +h }, colorScheme: scheme, deviceScaleFactor: 2 });
  await page.goto(url);
  await page.waitForFunction(() => window.ready === true);
  const end = await page.evaluate(() => window.replayEnd);
  for (let i = 0; i < +n; i++) {
    await page.evaluate(t => window.seek(t), end * i / (+n - 1));
    await page.screenshot({ path: path.join(out, `f${String(i).padStart(3, "0")}.png`), fullPage: true });
  }
  await browser.close();
  console.log(`${n} frames, ${end.toFixed(1)} s replayed → ${out}`);
})();
