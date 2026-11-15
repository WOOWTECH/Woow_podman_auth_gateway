const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const prefix='/api/woow_protected_apps/pi-agent/';
function bridge(){
 const calls=[];
 class Element { setAttribute(){} }
 class Document {}
 class CSSStyleDeclaration { setProperty(name,value){calls.push(value);} }
 class XHR { open(...args){calls.push(args[1]);} }
 const history={pushState(_s,_t,url){calls.push(url)},replaceState(_s,_t,url){calls.push(url)}};
 const window={fetch(url){calls.push(url)},WebSocket:class{},EventSource:class{}};
 const navigator={serviceWorker:{register(url,options){calls.push({url,options});}}};
 const context={URL,Request,navigator,window,history,location:{href:'https://ha.example'+prefix,host:'ha.example'},XMLHttpRequest:XHR,CSSStyleDeclaration,Element,Document,document:new Document()};
 for(const name of ['HTMLScriptElement','HTMLLinkElement','HTMLImageElement','HTMLAnchorElement','HTMLFormElement'])context[name]=class extends Element{};
 const script=fs.readFileSync('ha/custom_components/woow_protected_apps/frontend/ingress-bridge.js','utf8').replace('__PREFIX__',JSON.stringify(prefix)).replace('__TARGET__','"pi-agent"');
 vm.runInNewContext(script,context);
 return {window,history,calls,CSSStyleDeclaration,navigator};
}
test('Next root normalization and query navigation never double-prefix ingress',()=>{
 const {window,history,calls}=bridge();
 window.fetch(prefix.slice(0,-1)+'?cwd=test&_rsc=123');
 window.fetch(prefix+'api/sessions');
 window.fetch('/api/sessions');
 history.replaceState({},'',prefix.slice(0,-1)+'?session=abc');
 assert.deepEqual(calls,[
  'https://ha.example'+prefix+'?cwd=test&_rsc=123',
  'https://ha.example'+prefix+'api/sessions',
  'https://ha.example'+prefix+'api/sessions',
  'https://ha.example'+prefix+'?session=abc']);
});
test('external identity providers and non-network URLs stay untouched',()=>{
 const {window,calls}=bridge();
 for(const url of ['https://login.tailscale.com/','data:text/plain,test','mailto:test@example.com'])window.fetch(url);
 assert.deepEqual(calls,['https://login.tailscale.com/','data:text/plain,test','mailto:test@example.com']);
});

test('CSS custom-property icon URLs are scoped',()=>{
 const {CSSStyleDeclaration,calls}=bridge();
 new CSSStyleDeclaration().setProperty('--icon','url(/icons/file.svg)');
 assert.deepEqual(calls,['url(https://ha.example'+prefix+'icons/file.svg)']);
});

test('application service worker is scoped under ingress, never HA root',()=>{
 const {navigator,calls}=bridge();
 navigator.serviceWorker.register('/sw.js',{scope:'/',updateViaCache:'none'});
 assert.deepEqual(JSON.parse(JSON.stringify(calls)),[{url:'https://ha.example'+prefix+'sw.js',options:{scope:'https://ha.example'+prefix,updateViaCache:'none'}}]);
});
