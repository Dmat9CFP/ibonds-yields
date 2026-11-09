#!/usr/bin/env node
/**
 * Headless checks for ladder.html: load with no console errors, refresh only
 * on click, schema rejection, and unchanged math for the same yields.
 *
 *   node tests/test_ladder_browser.mjs
 * Requires Google Chrome and puppeteer-core (npm install puppeteer-core).
 */
import { createRequire } from "node:module";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pageUrl = pathToFileURL(path.join(root, "ladder.html")).href;
const YIELD_URL = "https://raw.githubusercontent.com/Dmat9CFP/ibonds-yields/main/data/yields.json";
const chrome = process.env.CHROME || "/usr/local/bin/google-chrome";

function loadPuppeteer() {
  const candidates = ["puppeteer-core", "/tmp/node_modules/puppeteer-core"];
  for (const name of candidates) {
    try { return require(name); } catch { /* next */ }
  }
  throw new Error("puppeteer-core is not installed (npm install puppeteer-core)");
}

const puppeteer = loadPuppeteer();
const failures = [];
function check(cond, message) {
  if (!cond) failures.push(message);
  else console.log("ok", message);
}

const queue = [];
const requests = [];
let phase = "load";

const userData = await mkdtemp(path.join(tmpdir(), "ladder-chrome-"));
const browser = await puppeteer.launch({
  executablePath: chrome,
  headless: true,
  userDataDir: userData,
  args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--allow-file-access-from-files"],
});
const page = await browser.newPage();
const consoleErrors = [];
const pageErrors = [];
page.on("pageerror", (err) => pageErrors.push(String(err)));
page.on("console", (msg) => {
  if (msg.type() !== "error") return;
  const text = msg.text();
  if (/favicon/i.test(text)) return;
  consoleErrors.push(text);
});
await page.setRequestInterception(true);
page.on("request", (req) => {
  const url = req.url();
  if (url.startsWith("file:") || url.startsWith("data:") || url.startsWith("blob:")) return req.continue();
  if (url.endsWith("/favicon.ico")) return req.abort();
  const rec = { phase, url, method: req.method(), headers: req.headers(), postData: req.postData() || null };
  requests.push(rec);
  if (url === YIELD_URL && phase === "timeout") return;
  if (url === YIELD_URL && phase === "http") {
    return req.respond({ status: 503, contentType: "text/plain", headers: { "access-control-allow-origin": "*" }, body: "no" });
  }
  if (url === YIELD_URL && queue.length) {
    const body = queue.shift();
    return req.respond({
      status: 200,
      contentType: "application/json; charset=utf-8",
      headers: { "access-control-allow-origin": "*" },
      body,
    });
  }
  return req.abort();
});

try {
  await page.goto(pageUrl, { waitUntil: "load", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 700));
  check(pageErrors.length === 0, "no page errors on load: " + pageErrors.join(" | "));
  check(consoleErrors.length === 0, "no console errors on load: " + consoleErrors.join(" | "));
  check(requests.length === 0, "no network requests before Refresh yields is clicked");

  const csp = await page.$eval('meta[http-equiv="Content-Security-Policy"]', (el) => el.content);
  check(
    csp === "connect-src https://raw.githubusercontent.com; form-action 'none'; img-src 'self' data: blob:; base-uri 'none'",
    "CSP meta is exact",
  );
  check(await page.$("#btnRefreshYields"), "Refresh yields button is present");
  check(await page.$("#fileIn") && await page.$("#drop"), "manual import fallback is still present");
  const badge = await page.$eval("#asOfBadge", (el) => el.textContent);
  check(badge.includes("Yields as of") && badge.includes("2026"), "as-of badge shows the embedded date");

  const schema = await page.evaluate(() => {
    const base = JSON.parse(document.getElementById("embedded-yields").textContent);
    const problems = [];
    const ok = (label, doc) => {
      const v = window.__ladder.validateYields(doc);
      if (!v.ok) problems.push(label + " should pass: " + v.error);
    };
    const bad = (label, doc) => {
      const v = window.__ladder.validateYields(doc);
      if (v.ok) problems.push(label + " should fail");
    };
    ok("embedded", base);
    const ytmOk = structuredClone(base);
    ytmOk.tips.funds[0].ytm = 14.99;
    ok("high but sane ytm", ytmOk);
    const missing = structuredClone(base);
    delete missing.asOf;
    bad("missing asOf", missing);
    const date = structuredClone(base);
    date.asOf = "2026-02-31";
    bad("impossible date", date);
    const str = structuredClone(base);
    str.tips.funds[0].enay = "4.5";
    bad("string yield", str);
    const huge = structuredClone(base);
    huge.tips.funds[0].ytm = 50;
    bad("yield 50", huge);
    const edge = structuredClone(base);
    edge.corporate.funds[0].enay = 15;
    bad("yield 15", edge);
    const neg = structuredClone(base);
    neg.corporate.funds[0].enay = -2;
    bad("yield -2", neg);
    const tick = structuredClone(base);
    tick.tips.funds[0].ticker = "IBGX";
    bad("unknown ticker", tick);
    const extra = structuredClone(base);
    extra.hello = 1;
    bad("extra field", extra);
    const real = structuredClone(base);
    delete real.tips.funds[0].realYield;
    bad("missing real yield", real);
    const empty = structuredClone(base);
    empty.muni.funds = [];
    bad("empty funds", empty);
    const name = structuredClone(base);
    name.tips.funds[0].name = "<img src=x onerror=alert(1)>";
    bad("html name", name);
    const notes = structuredClone(base);
    notes.notes = "x".repeat(5000);
    bad("oversized notes", notes);
    const pj = structuredClone(base);
    pj.pjtxx.ticker = "SPY";
    bad("pjtxx ticker", pj);
    const dup = structuredClone(base);
    dup.tips.funds.push(structuredClone(dup.tips.funds[0]));
    bad("duplicate ticker", dup);
    const proto = JSON.parse(JSON.stringify(base).replace("{", "{\"__proto__\":{\"polluted\":true},"));
    bad("proto key", proto);
    return problems;
  });
  check(schema.length === 0, "schema validator cases: " + schema.join("; "));

  const imported = await page.evaluate(async () => {
    const raw = document.getElementById("embedded-yields").textContent;
    const before = window.__ladder.Y.tips.funds[0].enay;
    const msg = await window.__ladder.importBuffer(new TextEncoder().encode(raw), "yields.json");
    let rejected = false;
    try { await window.__ladder.importBuffer(new TextEncoder().encode('{"asOf":"2026-11-06","notes":"x"}'), "bad.json"); }
    catch (e) { rejected = /rejected/i.test(String(e && e.message)); }
    return { msg, same: window.__ladder.Y.tips.funds[0].enay === before, rejected };
  });
  check(imported.same && /updated yields/.test(imported.msg), "manual import accepts a yields.json file: " + imported.msg);
  check(imported.rejected, "manual import rejects a yields file that fails the schema");

  await page.evaluate(() => {
    window.__alerted = false;
    window.alert = () => { window.__alerted = true; };
    const nick = document.querySelector("[data-h=nick]");
    if (nick) nick.value = "SECRET-NICK";
    const acct = document.getElementById("acct");
    acct.value = "9876543";
    acct.dispatchEvent(new Event("input", { bubbles: true }));
  });

  const fp = () => page.evaluate(() => {
    const R = window.__ladder.compute(window.__ladder.S, window.__ladder.Y);
    return JSON.stringify({
      asOf: window.__ladder.Y.asOf,
      cash: +R.cash.toFixed(4),
      ladder: +R.ladder.toFixed(4),
      eq: +R.eq.toFixed(4),
      alts: +R.alts.toFixed(4),
      rows: R.rows.map((r) => [r.ticker, +r.purchase.toFixed(4), r.enay ?? null, +(+r.yieldUsed).toFixed(6)]),
    });
  });

  const embedded = await page.evaluate(() => document.getElementById("embedded-yields").textContent);
  const before = await fp();
  const diag = await page.evaluate(() => ({
    empty: window.__ladder.acceptYieldResponseUrl(""),
    good: window.__ladder.acceptYieldResponseUrl(window.__ladder.YIELD_URL),
    evil: window.__ladder.acceptYieldResponseUrl("https://example.com/data/yields.json"),
    redirect: window.__ladder.YIELD_REDIRECT,
    timeout: window.__ladder.describeRefreshFailure(new DOMException("The operation was aborted.", "AbortError"), ""),
    fetchErr: window.__ladder.describeRefreshFailure(new TypeError("Failed to fetch"), ""),
    csp: window.__ladder.describeRefreshFailure(new TypeError("Failed to fetch"), "connect-src blocked https://example.com/exfil"),
  }));
  check(diag.empty === true && diag.good === true && diag.evil === false, "empty file:// response URL is accepted and other hosts are not");
  check(diag.redirect === "error", "refresh does not follow redirects");
  check(diag.timeout.startsWith("Timeout:") && diag.timeout.includes("15s"), "abort is reported as a 15s timeout: " + diag.timeout);
  check(diag.fetchErr.startsWith("Fetch: TypeError:"), "fetch TypeError keeps its name: " + diag.fetchErr);
  check(diag.csp.startsWith("CSP: connect-src blocked"), "CSP diagnosis names the directive: " + diag.csp);
  phase = "refresh-same";
  queue.push(embedded);
  await page.click("#btnRefreshYieldsTop");
  await page.waitForFunction(() => {
    const t = document.getElementById("refreshStatus").textContent;
    return t && !t.startsWith("Refreshing");
  });
  const afterSame = await fp();
  const sameMsg = await page.$eval("#refreshStatus", (el) => el.textContent);
  const panelOpen = await page.$eval("#yieldsPanel", (el) => el.open);
  check(before === afterSame, "same yields leave calculations unchanged");
  check(sameMsg.includes("Yields updated"), "header status shows success: " + sameMsg);
  check(panelOpen === false, "success does not depend on the Yields panel being open");
  const sameReq = requests.filter((r) => r.phase === "refresh-same");
  check(sameReq.length === 1 && sameReq[0].method === "GET", "button sends one GET");
  check(sameReq[0] && sameReq[0].url === YIELD_URL && !sameReq[0].url.includes("?"), "GET url has no query string");
  check(sameReq[0] && !sameReq[0].postData, "GET has no body");
  const hdr = sameReq[0] ? sameReq[0].headers : {};
  check(!hdr.cookie && !hdr.authorization && !hdr.referer && !hdr.referrer, "no cookie, authorization, or referrer header");
  check(!JSON.stringify(sameReq[0]).includes("9876543") && !JSON.stringify(sameReq[0]).includes("SECRET-NICK"), "typed values are not on the request");

  const newer = JSON.parse(embedded);
  for (const series of ["corporate", "tips", "treasury", "muni"]) newer[series].enayAsOf = "2026-11-06";
  newer.tips.realYieldAsOf = "2026-11-06";
  newer.asOf = "2026-11-06";
  newer.tips.funds[0].enay = 1.11;
  phase = "refresh-newer";
  queue.push(JSON.stringify(newer));
  await page.$eval("#btnRefreshYields", (el) => el.click());
  await page.waitForFunction(() => document.getElementById("refreshStatus").textContent.includes("Yields updated"));
  const changed = await fp();
  check(changed !== before, "a changed yield changes the ladder");
  check(JSON.parse(changed).asOf === "2026-11-06", "fetched asOf newer than embedded is applied");

  phase = "reload-newer";
  const reqsBeforeReload = requests.length;
  await page.reload({ waitUntil: "load", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 400));
  const kept = await fp();
  check(JSON.parse(kept).asOf === "2026-11-06" && JSON.parse(kept).rows.some((r) => r[2] === 1.11), "fetched asOf newer than embedded still wins after reload");
  check(requests.length === reqsBeforeReload, "reload does not fetch yields");

  const malformedFp = await fp();
  await page.evaluate(() => { window.__alerted = false; window.alert = () => { window.__alerted = true; }; });
  phase = "malformed";
  queue.push('{"not":"yields","<img>":true}');
  await page.click("#asOfBadge");
  await page.waitForFunction(() => document.getElementById("refreshStatus").textContent.startsWith("Validation:"));
  const afterBad = await fp();
  const badMsg = await page.$eval("#refreshStatus", (el) => el.textContent);
  check(afterBad === malformedFp, "malformed JSON is not applied");
  check(!badMsg.includes("<img>") && !badMsg.includes('"not"'), "failure message does not echo the payload");
  check(await page.evaluate(() => {
    window.alert = () => { window.__alerted = true; };
    return document.querySelectorAll("img").length === 0 && window.__alerted !== true;
  }), "malformed payload is not injected as HTML");

  phase = "oversize";
  queue.push("{" + "A".repeat(300000));
  await page.evaluate(() => window.__ladder.refreshYields());
  await page.waitForFunction(() => /too large/i.test(document.getElementById("refreshStatus").textContent));
  check(await fp() === malformedFp, "oversized response is not applied");
  phase = "json";
  queue.push("not-json{{{");
  await page.evaluate(() => window.__ladder.refreshYields());
  await page.waitForFunction(() => document.getElementById("refreshStatus").textContent.startsWith("JSON:"));
  check(await fp() === malformedFp, "invalid JSON is not applied");
  const jsonMsg = await page.$eval("#refreshStatus", (el) => el.textContent);
  check(!jsonMsg.includes("not-json"), "JSON failure does not echo the body");
  phase = "http";
  await page.evaluate(() => window.__ladder.refreshYields());
  await page.waitForFunction(() => /HTTP 503/.test(document.getElementById("refreshStatus").textContent));
  check(await fp() === malformedFp, "non-200 response is not applied");

  const older = JSON.parse(embedded);
  older.asOf = "2020-01-15";
  for (const series of ["corporate", "tips", "treasury", "muni"]) older[series].enayAsOf = "2020-01-15";
  older.tips.realYieldAsOf = "2020-01-15";
  older.tips.funds[0].enay = 9.5;
  phase = "older";
  queue.push(JSON.stringify(older));
  await page.evaluate(() => window.__ladder.refreshYields());
  await page.waitForFunction(() => /older than this file/i.test(document.getElementById("refreshStatus").textContent));
  check(await fp() === malformedFp, "a download older than the embedded file is not applied");

  phase = "stored-old";
  await page.evaluate((raw) => localStorage.setItem("ladderTool.yields.v1", raw), JSON.stringify(older));
  await page.reload({ waitUntil: "load", timeout: 30000 });
  const restored = await page.evaluate(() => window.__ladder.Y.asOf + " " + window.__ladder.Y.tips.funds[0].enay);
  check(restored.startsWith("2026-10-22"), "embedded yields win when they are newer than stored: " + restored);

  const blocked = await page.evaluate(async () => {
    let fetchBlocked = false;
    try { await fetch("https://example.com/exfil"); } catch { fetchBlocked = true; }
    const img = await new Promise((resolve) => {
      const el = new Image();
      el.onload = () => resolve("loaded");
      el.onerror = () => resolve("blocked");
      el.src = "https://example.com/beacon.png";
      setTimeout(() => resolve("timeout"), 800);
    });
    return { fetchBlocked, img };
  });
  check(blocked.fetchBlocked, "CSP blocks connect to other hosts");
  check(blocked.img !== "loaded", "CSP blocks image beacons");
  await page.waitForFunction(() => (window.__yieldCspLog || []).some((t) => /connect-src blocked https:\/\/example.com/.test(t)));
  check(true, "CSP listener records the blocked URI and directive");
  await page.click("#btnCopyDiag");
  const copied = await page.evaluate(() => window.__yieldCopied || "");
  check(copied.includes(await page.evaluate(() => navigator.userAgent)) && !copied.includes("9876543") && !copied.includes("SECRET-NICK"), "copy diagnostics is the browser UA and the error, with no typed values");
  check(pageErrors.length === 0, "no page errors after refresh: " + pageErrors.join(" | "));
  await page.evaluate(() => {
    window.dispatchEvent(new ErrorEvent("error", {message: "probe-script-error", error: new Error("probe-script-error")}));
  });
  await page.waitForFunction(() => /probe-script-error/.test(document.getElementById("refreshStatus").textContent));
  check(true, "a script error is shown in the header status line");
  phase = "timeout";
  await page.evaluate(() => window.__ladder.refreshYields());
  await page.waitForFunction(() => document.getElementById("refreshStatus").textContent.startsWith("Timeout:"), { timeout: 20000 });
  check(await fp() === await fp(), "timeout leaves the page usable");
} catch (err) {
  failures.push(err && err.stack ? err.stack : String(err));
  try {
    const status = await page.$eval("#refreshStatus", (el) => el.textContent);
    failures.push("status at failure: " + status);
  } catch { /* page already gone */ }
} finally {
  await browser.close();
  await rm(userData, { recursive: true, force: true });
}

if (failures.length) {
  console.error("\nFAILED");
  for (const f of failures) console.error(" - " + f);
  process.exit(1);
}
console.log("\nAll browser checks passed");
