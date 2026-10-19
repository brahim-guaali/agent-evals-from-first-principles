// One screenshot of a viewer page: node viz/screenshot.js <url> <out.png> [width] [height] [wait ms]
const { chromium } = require(process.env.PLAYWRIGHT || "playwright");
(async () => {
  const [url, out, w = "1200", h = "900", wait = "1500"] = process.argv.slice(2);
  const browser = await chromium.launch(process.env.CHROME_CHANNEL ? { channel: process.env.CHROME_CHANNEL } : {});
  const page = await browser.newPage({ viewport: { width: +w, height: +h }, deviceScaleFactor: 2 });
  await page.goto(url);
  await page.waitForTimeout(+wait);
  await page.screenshot({ path: out, fullPage: true });
  await browser.close();
  console.log(out);
})();
