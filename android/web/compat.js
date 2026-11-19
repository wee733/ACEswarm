import 'core-js/modules/es.array.at.js';
import 'core-js/modules/es.string.pad-start.js';
import 'core-js/modules/es.string.replace-all.js';
import 'core-js/modules/es.object.entries.js';
import 'core-js/modules/es.object.values.js';
import 'core-js/modules/es.object.from-entries.js';
import 'core-js/modules/es.object.get-own-property-descriptors.js';

// DOM and ECMAScript methods used by the shared desktop on the H16's WebView 52.
for (const proto of [Element.prototype, Document.prototype, DocumentFragment.prototype]) {
  if (!proto.append) proto.append = function (...nodes) {
    for (const node of nodes) this.appendChild(node instanceof Node ? node : document.createTextNode(node));
  };
  if (!proto.prepend) proto.prepend = function (...nodes) {
    const fragment = document.createDocumentFragment();
    fragment.append(...nodes);
    this.insertBefore(fragment, this.firstChild);
  };
  if (!proto.replaceChildren) proto.replaceChildren = function (...nodes) {
    while (this.firstChild) this.removeChild(this.firstChild);
    this.append(...nodes);
  };
}
if (!NodeList.prototype[Symbol.iterator]) NodeList.prototype[Symbol.iterator] = Array.prototype[Symbol.iterator];
if (!NodeList.prototype.forEach) NodeList.prototype.forEach = Array.prototype.forEach;
