class WoowAuthIframeCard extends HTMLElement {
  setConfig(config) {
    if (!config.target || !["pi-agent", "tailscale-web"].includes(config.target)) {
      throw new Error("target must be pi-agent or tailscale-web");
    }
    this._config = config;
    this._start();
  }

  set hass(hass) {
    this._hass = hass;
    this._start();
  }

  getCardSize() {
    return 8;
  }

  async _start() {
    if (!this._hass || !this._config || this._started) return;
    this._started = true;
    this.innerHTML = `
      <style>
        :host { display: block; height: calc(100vh - var(--header-height, 56px)); }
        .status { display: grid; place-items: center; height: 100%; color: var(--primary-text-color); }
        iframe { display: block; width: 100%; height: 100%; border: 0; background: var(--card-background-color); }
      </style>
      <div class="status">正在透過 Home Assistant 驗證…</div>`;
    try {
      const result = await this._hass.callWS({
        type: "woow_ingress/create_ticket",
        target: this._config.target,
      });
      const url = new URL(window.location.href);
      url.port = String(result.port);
      url.pathname = "/auth/launch";
      url.search = new URLSearchParams({ ticket: result.ticket }).toString();
      url.hash = "";
      const iframe = document.createElement("iframe");
      iframe.src = url.toString();
      iframe.title = this._config.title || this._config.target;
      iframe.allow = "clipboard-read; clipboard-write";
      iframe.referrerPolicy = "origin";
      this.querySelector(".status").replaceWith(iframe);
    } catch (error) {
      this._started = false;
      const message = error?.message || String(error);
      this.querySelector(".status").textContent = `Home Assistant 驗證失敗：${message}`;
    }
  }
}

if (!customElements.get("woow-auth-iframe-card")) {
  customElements.define("woow-auth-iframe-card", WoowAuthIframeCard);
}
window.customCards = window.customCards || [];
window.customCards.push({
  type: "woow-auth-iframe-card",
  name: "WOOW HA Authenticated Iframe",
  description: "Launches an admin-only web application using a short-lived Home Assistant ticket.",
});
