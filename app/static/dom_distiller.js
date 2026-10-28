/**
 * dom_distiller.js — injected via page.addInitScript and via evaluate on each step.
 *
 * Queries visible interactive elements AND elements containing pricing/status data,
 * assigns data-agent-id="1..N", renders Set-of-Marks visual bounding boxes + numbered badges,
 * and returns structured JSON for the LLM.
 */
(function () {
  const MAX_ELEMENTS = 90;

  function ensureOverlay() {
    let c = document.getElementById("__rpa_som_overlay__");
    if (!c) {
      c = document.createElement("div");
      c.id = "__rpa_som_overlay__";
      c.style.cssText =
        "position:fixed;inset:0;z-index:2147483646;pointer-events:none;";
      document.documentElement.appendChild(c);
    }
    c.replaceChildren();
    return c;
  }

  function textOf(el) {
    let t = (
      el.innerText ||
      el.getAttribute("aria-label") ||
      el.getAttribute("placeholder") ||
      el.getAttribute("alt") ||
      ""
    )
      .replace(/\s+/g, " ")
      .trim();
    if (t.length > 100) t = t.slice(0, 97) + "...";
    return t;
  }

  function selectorHint(el) {
    const parts = [];
    if (el.id) parts.push(`#${el.id}`);
    if (el.getAttribute("name")) parts.push(`[name="${el.getAttribute("name")}"]`);
    const cls = (el.className || "").toString().split(/\s+/).filter(Boolean)[0];
    if (cls) parts.push(`.${cls}`);
    return parts.join("");
  }

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || st.opacity === "0")
      return false;
    // Discard elements far out of viewport
    if (r.bottom < -50 || r.right < -50 || r.top > (window.innerHeight || 900) * 1.8) return false;
    return true;
  }

  window.__rpa_distill__ = function () {
    const overlay = ensureOverlay();
    // Interactive controls + elements holding pricing & status badges
    const q = [
      'a[href]',
      'button',
      'input',
      'select',
      'textarea',
      '[role="button"]',
      '[role="link"]',
      '[tabindex]:not([tabindex="-1"])',
      '.price',
      '.badge',
      '[class*="price"]',
      '[class*="badge"]',
      '[class*="status"]',
      '[data-code]'
    ].join(', ');

    const raw = Array.from(document.querySelectorAll(q));
    const out = [];
    let agentId = 1;

    for (const el of raw) {
      if (out.length >= MAX_ELEMENTS) break;
      if (!isVisible(el)) continue;

      // Avoid duplicate marking for inner child if parent is already an interactive button/link
      const parentInteractive = el.parentElement ? el.parentElement.closest('button, a[href], [role="button"]') : null;
      if (parentInteractive && parentInteractive !== el && raw.includes(parentInteractive)) {
        continue;
      }

      const r = el.getBoundingClientRect();
      const idStr = String(agentId);
      el.setAttribute("data-agent-id", idStr);

      const isInput = el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT";
      const isAction = el.tagName === "BUTTON" || el.tagName === "A" || el.getAttribute("role") === "button" || el.onclick != null;

      // Set-of-Marks visual highlight
      const box = document.createElement("div");
      const borderColor = isAction ? "#2563eb" : (isInput ? "#9333ea" : "#16a34a");
      const badgeBg = isAction ? "#1d4ed8" : (isInput ? "#7e22ce" : "#15803d");

      box.style.cssText = [
        "position:absolute",
        `left:${Math.max(0, r.left)}px`,
        `top:${Math.max(0, r.top)}px`,
        `width:${r.width}px`,
        `height:${r.height}px`,
        `border:2px solid ${borderColor}`,
        "background:rgba(37,99,235,0.04)",
        "border-radius:5px",
        "box-sizing:border-box",
        "pointer-events:none",
      ].join(";");

      const badge = document.createElement("span");
      badge.textContent = idStr;
      badge.style.cssText = [
        "position:absolute",
        "left:0px",
        "top:-17px",
        "min-width:18px",
        "height:17px",
        "padding:0 4px",
        `background:${badgeBg}`,
        "#fff",
        "color:#fff",
        "font:bold 11px/17px monospace",
        "border-radius:3px",
        "text-align:center",
        "z-index:2147483647",
        "box-shadow:0 1px 3px rgba(0,0,0,0.3)",
      ].join(";");

      box.appendChild(badge);
      overlay.appendChild(box);

      out.push({
        id: agentId,
        tag: el.tagName.toLowerCase(),
        text: textOf(el),
        type: el.getAttribute("type") || null,
        href: el.getAttribute("href") || null,
        value: el.value != null ? String(el.value).slice(0, 120) : null,
        placeholder: el.getAttribute("placeholder") || null,
        selector: selectorHint(el),
        is_interactive: isAction || isInput,
        bbox: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
      });

      agentId++;
    }

    return out;
  };

  window.__rpa_clear_overlay__ = function () {
    document.getElementById("__rpa_som_overlay__")?.remove();
    document.querySelectorAll("[data-agent-id]").forEach((el) => el.removeAttribute("data-agent-id"));
  };
})();
