// ghostbox vault extension. The box daemon (boxd) asks, over native messaging, for three things:
//   front    -> the active tab's origin and its focused element
//   fill     -> check the active tab's origin and focused input, then set the value, in ONE
//               synchronous script in the extension's isolated world (the page cannot redefine
//               what that world sees, and cannot run between the check and the fill)
//   navigate -> open an http(s) URL in the active tab (like a bookmark: no keystrokes to intercept)
// This is how a password manager works; it replaces a DevTools connection, which marks the
// browser as automated (navigator.webdriver) for every page.
const HOST = "dev.ghostbox.vault";
let port = null;

function probe() {
  const a = document.activeElement;
  return {
    origin: location.origin, href: location.href,
    focused: document.hasFocus(), visible: document.visibilityState === "visible",
    tag: a ? a.tagName : null, type: a && "type" in a ? String(a.type) : null,
    readOnly: !!(a && (a.readOnly || a.disabled)),
    inFrame: !!(a && (a.tagName === "IFRAME" || a.tagName === "FRAME")),
  };
}

function fill(origin, types, value) {
  const a = document.activeElement;
  const ok = location.origin === origin && document.hasFocus() && a && a.tagName === "INPUT" &&
    !a.readOnly && !a.disabled && types.includes(String(a.type || "").toLowerCase());
  if (!ok) {
    return { ok: false, origin: location.origin, tag: a ? a.tagName : null, type: a && a.type };
  }
  // The isolated world's own HTMLInputElement setter (a page cannot patch it), then the events a
  // person's typing would end with, so frameworks see the new value.
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(a, value);
  a.dispatchEvent(new Event("input", { bubbles: true }));
  a.dispatchEvent(new Event("change", { bubbles: true }));
  return { ok: true, origin: location.origin };
}

async function activeTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs.length !== 1) throw new Error("no single active tab");
  return tabs[0];
}

async function handle(msg) {
  const tab = await activeTab();
  if (msg.op === "front") {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: probe });
    return r.result;
  }
  if (msg.op === "fill") {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, func: fill, args: [msg.origin, msg.types, msg.value] });
    return r.result;
  }
  if (msg.op === "navigate") {
    const u = new URL(msg.url);
    if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("http(s) only");
    await chrome.tabs.update(tab.id, { url: u.href });
    return { ok: true };
  }
  if (msg.op === "save") {          // save what the tab shows (e.g. a PDF in the built-in viewer)
    const u = new URL(tab.url);
    if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("http(s) only");
    const id = await chrome.downloads.download({ url: u.href, conflictAction: "uniquify" });
    return { ok: true, downloadId: id };
  }
  throw new Error("unknown op");
}

function connect() {
  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener(async (msg) => {
    let reply;
    try {
      reply = { id: msg.id, result: await handle(msg) };
    } catch (e) {
      reply = { id: msg.id, error: String(e && e.message || e) };
    }
    try { port.postMessage(reply); } catch (e) {}
  });
  port.onDisconnect.addListener(() => { port = null; setTimeout(connect, 2000); });
}
connect();
chrome.runtime.onStartup.addListener(() => { if (!port) connect(); });
