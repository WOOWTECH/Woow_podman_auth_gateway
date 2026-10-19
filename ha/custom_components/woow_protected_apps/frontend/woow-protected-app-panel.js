// Sidebar panel for one protected WOOW web app (registered by woow_protected_apps).
//
// The toolbar is drawn here, not by HA, so it carries only HA's menu button (shown when the
// sidebar is collapsed or the screen is narrow) and the app's name. Below it, an iframe opens
// the app through woow-auth-gateway with a one-use ticket from HA.
const RETRY_MS = 10000;

class WoowProtectedAppPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" }).innerHTML = `
      <style>
        :host { display: flex; flex-direction: column; height: 100%; background: var(--primary-background-color); }
        .toolbar {
          display: flex; align-items: center; box-sizing: border-box; flex: none;
          height: calc(var(--header-height, 56px) + var(--safe-area-inset-top, 0px));
          padding: var(--safe-area-inset-top, 0px) 12px 0;
          background-color: var(--app-header-background-color);
          color: var(--app-header-text-color, white);
          border-bottom: var(--app-header-border-bottom, none);
          font-size: var(--ha-font-size-xl, 20px);
          font-weight: var(--ha-font-weight-normal, 400);
        }
        .title { margin-inline-start: 24px; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .content { flex: 1; min-height: 0; }
        .status { display: grid; place-items: center; height: 100%; padding: 16px; box-sizing: border-box; color: var(--primary-text-color); }
        iframe { display: block; width: 100%; height: 100%; border: 0; background: var(--card-background-color); }
      </style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title"></div>
      </div>
      <div class="content"><div class="status">正在透過 Home Assistant 驗證…</div></div>`;
  }

  set hass(hass) {
    this._hass = hass;
    this.shadowRoot.querySelector("ha-menu-button").hass = hass;
    this._start();
  }

  set narrow(narrow) {
    this.shadowRoot.querySelector("ha-menu-button").narrow = narrow;
  }

  set panel(panel) {
    this._panel = panel;
    this.shadowRoot.querySelector(".title").textContent = panel.title;
    this._start();
  }

  async _start() {
    if (!this._hass || !this._panel || this._started) return;
    // After a failure, retry on a later hass update, but not on every one of them.
    if (Date.now() - (this._failedAt || 0) < RETRY_MS) return;
    this._started = true;
    const target = this._panel.config.target;
    const content = this.shadowRoot.querySelector(".content");
    try {
      const result = await this._hass.callWS({
        type: "woow_protected_apps/create_ticket",
        target,
      });
      const url = new URL(window.location.href);
      url.port = String(result.port);
      url.pathname = "/auth/launch";
      url.search = new URLSearchParams({ ticket: result.ticket }).toString();
      url.hash = "";
      const iframe = document.createElement("iframe");
      iframe.src = url.toString();
      iframe.title = this._panel.title || target;
      iframe.allow = "clipboard-read; clipboard-write";
      iframe.referrerPolicy = "origin";
      content.replaceChildren(iframe);
    } catch (error) {
      this._started = false;
      this._failedAt = Date.now();
      const message = error?.message || String(error);
      content.querySelector(".status").textContent = `Home Assistant 驗證失敗：${message}`;
    }
  }
}

if (!customElements.get("woow-protected-app-panel")) {
  customElements.define("woow-protected-app-panel", WoowProtectedAppPanel);
}
