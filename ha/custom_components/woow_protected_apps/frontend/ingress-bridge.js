// Scoped to an ingress iframe, never installed on the HA parent window.
(() => {
  const prefix = __PREFIX__;
  const target = __TARGET__;
  function map(value) {
    if (typeof value !== 'string') return value;
    try {
      const u = new URL(value, location.href);
      if (u.host !== location.host || !['http:','https:','ws:','wss:'].includes(u.protocol)) return value;
      // Next normalizes the mounted root by removing its trailing slash.
      // Treat that root as already scoped, otherwise it is prefixed twice.
      if (u.pathname === prefix.slice(0, -1)) u.pathname = prefix;
      else if (!u.pathname.startsWith(prefix)) u.pathname = prefix + u.pathname.replace(/^\//, '');
      return u.href;
    } catch (_) { return value; }
  }
  const originalFetch = window.fetch;
  window.fetch = function(input, init) {
    if (input instanceof Request) input = new Request(map(input.url), input);
    else input = map(String(input));
    return originalFetch.call(this, input, init);
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {return open.call(this, method, map(String(url)), ...args);};
  for (const name of ['WebSocket','EventSource']) {
    const Original = window[name];
    window[name] = new Proxy(Original, {construct(Type, args) {args[0] = map(String(args[0])); return Reflect.construct(Type,args);}});
  }
  for (const name of ['pushState','replaceState']) {
    const original = history[name];
    history[name] = function(state, unused, url) {return original.call(this,state,unused,url == null ? url : map(String(url)));};
  }
  for (const [Type, prop] of [[HTMLScriptElement,'src'],[HTMLLinkElement,'href'],[HTMLImageElement,'src'],[HTMLAnchorElement,'href'],[HTMLFormElement,'action']]) {
    const d = Object.getOwnPropertyDescriptor(Type.prototype,prop);
    if (d?.set) Object.defineProperty(Type.prototype,prop,{...d,set(v){d.set.call(this,map(String(v)));}});
  }
  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(name, value) {
    if (['src','href','action'].includes(name.toLowerCase())) value = map(String(value));
    return setAttribute.call(this,name,value);
  };
  // React file icons use CSS custom properties, rather than img.src.
  const setProperty = CSSStyleDeclaration.prototype.setProperty;
  CSSStyleDeclaration.prototype.setProperty = function(name, value, priority) {
    if (typeof value === 'string') value = value.replace(/url\(\s*(["']?)([^)"']+)\1\s*\)/g,
      (_match, quote, url) => 'url(' + quote + (url.startsWith('#') ? url : map(url.trim())) + quote + ')');
    return setProperty.call(this, name, value, priority);
  };
  // A Pi Web worker must never replace HA's worker at the origin root.
  if (navigator.serviceWorker) {
    const register = navigator.serviceWorker.register.bind(navigator.serviceWorker);
    navigator.serviceWorker.register = (url, options = {}) => register(map(String(url)), {
      ...options, scope: map(options.scope == null ? prefix : String(options.scope)),
    });
  }
  // Backend cookies are namespaced at the proxy; apps see their original names.
  const cookie = Object.getOwnPropertyDescriptor(Document.prototype,'cookie');
  if (cookie) Object.defineProperty(document,'cookie',{
    get() {return cookie.get.call(document).split('; ').filter(x=>x.startsWith('app_'+target+'_')).map(x=>x.slice(('app_'+target+'_').length)).join('; ');},
    set(value) {const parts=value.split(';');parts[0]='app_'+target+'_'+parts[0];cookie.set.call(document,parts.filter(x=>!/^\s*(path|domain)=/i.test(x)).join(';')+'; Path='+prefix+'; SameSite=Strict');}
  });
})();
