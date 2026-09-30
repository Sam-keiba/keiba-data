// jsc（JavaScriptCore）でカスタムコンポーネントを**実際に動かす**ための最小のDOM。
//
// ブラウザは使えないが、「クリアを押したらチェックが外れる」のような**操作の結果**は
// ソースの字面を見ても確かめられない。そこで、コンポーネントが使う分だけの
// createElement / getElementById / addEventListener を用意して、`fire()` で
// イベントを起こせるようにしてある。
//
// 足りない機能は**わざと**入れていない（本物のDOMの代わりにはならない）。
// コンポーネントが新しいAPIを使い始めたら、そのときここに足す。

function Node(tag) {
  this.tagName = tag;
  this.attrs = {};
  this.children = [];
  this.listeners = {};
  this._text = "";
  this.checked = false;
  this.value = "";
  this.offsetWidth = 0;
  this.clientWidth = 200;
  this.scrollHeight = 400;
  this.parentElement = null;
  this.dataset = {};
  this.offsetParent = this;      // 画面に出ている扱い（隠れている場合はテストで null にする）
  const self = this;
  // style は `_p` に貯める。よく使うものは `el.style.height = …` と書けるようにしておく
  this.style = { _p: {}, setProperty: function (k, v) { self.style._p[k] = v; } };
  ["height", "width", "left", "top", "right", "bottom", "transform", "display",
   "background", "color", "borderColor", "opacity"].forEach(function (name) {
    Object.defineProperty(self.style, name, {
      get: function () { return self.style._p[name] || ""; },
      set: function (v) { self.style._p[name] = v; },
    });
  });
  this.classList = {
    add: function (c) {
      const v = (self.attrs["class"] || "").split(" ").filter(Boolean);
      if (v.indexOf(c) < 0) v.push(c);
      self.attrs["class"] = v.join(" ");
    },
    remove: function (c) {
      const v = (self.attrs["class"] || "").split(" ").filter(Boolean);
      self.attrs["class"] = v.filter(function (x) { return x !== c; }).join(" ");
    },
    contains: function (c) {
      return (self.attrs["class"] || "").split(" ").indexOf(c) >= 0;
    },
    toggle: function (c, on) {
      if (on === undefined) on = !this.contains(c);
      if (on) this.add(c); else this.remove(c);
      return on;
    },
  };
}
Object.defineProperty(Node.prototype, "textContent", {
  get: function () { return this._text; },
  set: function (v) { this.children = []; this._text = v; },
});
Object.defineProperty(Node.prototype, "lastChild", {
  get: function () { return this.children[this.children.length - 1] || null; },
});
/** 親子をつなぐ（`closest` と `parentElement` に要る）。 */
function _adopt(parent, child) {
  if (child && typeof child !== "string") child.parentElement = parent;
  return child;
}
/** `<div class="a"><span class="b"></span></div>` くらいの単純なHTMLだけ読む。 */
function parseHTML(html) {
  const out = [];
  const stack = [];
  const tag = /<(\/?)(\w+)([^>]*)>/g;
  let m;
  while ((m = tag.exec(html))) {
    if (m[1]) {
      const done = stack.pop();
      if (!stack.length && done) out.push(done);
      continue;
    }
    const node = new Node(m[2]);
    const attr = /(\w[\w-]*)="([^"]*)"/g;
    let a;
    while ((a = attr.exec(m[3]))) node.attrs[a[1]] = a[2];
    if (stack.length) stack[stack.length - 1].appendChild(node);
    stack.push(node);
  }
  while (stack.length) {
    const done = stack.pop();
    if (!stack.length && done) out.push(done);
  }
  return out;
}
Object.defineProperty(Node.prototype, "innerHTML", {
  get: function () { return this._html || ""; },
  set: function (v) {
    this._html = v;
    this.children = [];
    const self = this;
    parseHTML(v).forEach(function (child) { self.appendChild(child); });
  },
});
Object.defineProperty(Node.prototype, "className", {
  get: function () { return this.attrs["class"] || ""; },
  set: function (v) { this.attrs["class"] = v; },
});
Object.defineProperty(Node.prototype, "title", {
  get: function () { return this.attrs.title || ""; },
  set: function (v) { this.attrs.title = v; },
});
/**
 * ごく単純なセレクタに答える。使えるのは
 * `.class` / `tag` / `[attr="値"]` と、その組み合わせ（`.lane[data-tier="A"]`）だけ。
 * `data-…` は `dataset` に入れた値も見る（コンポーネントが `el.dataset.x = …` と書くため）。
 */
Node.prototype.matches = function (selector) {
  const self = this;
  let rest = selector.trim();
  const head = /^([.#]?[\w-]+)/.exec(rest);
  if (head) {
    const token = head[1];
    rest = rest.slice(token.length);
    if (token.charAt(0) === ".") {
      if ((this.attrs["class"] || "").split(" ").indexOf(token.slice(1)) < 0) return false;
    } else if (token.charAt(0) === "#") {
      if (this.attrs.id !== token.slice(1)) return false;
    } else if (this.tagName !== token) {
      return false;
    }
  }
  const attr = /\[([\w-]+)="([^"]*)"\]/g;
  let m;
  let ok = true;
  while ((m = attr.exec(rest))) {
    const name = m[1];
    const value = self.attrs[name] != null ? self.attrs[name]
      : (name.indexOf("data-") === 0 ? self.dataset[name.slice(5)] : undefined);
    if (value !== m[2]) ok = false;
  }
  return ok;
};
Node.prototype.closest = function (selector) {
  let node = this;
  while (node) {
    if (node.matches && node.matches(selector)) return node;
    node = node.parentElement;
  }
  return null;
};
Node.prototype.querySelector = function (selector) {
  return this.find(function (c) { return c.matches(selector); });
};
Node.prototype.querySelectorAll = function (selector) {
  return this.findAll(function (c) { return c.matches(selector); });
};
Node.prototype.removeEventListener = function (type, fn) {
  this.listeners[type] = (this.listeners[type] || []).filter(function (f) { return f !== fn; });
};
Node.prototype.setPointerCapture = function () {};
Node.prototype.focus = function () { this.focused = true; };
Node.prototype.scrollIntoView = function () { this.scrolledIntoView = true; };
Node.prototype.getBoundingClientRect = function () {
  return this.rect || { left: 0, top: 0, width: 100, height: 100 };
};
Node.prototype.setAttribute = function (k, v) {
  if (k === "value") { this.value = v; return; }
  this.attrs[k] = v;
};
Node.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
Node.prototype.appendChild = function (n) {
  if (n && n.parentElement && n.parentElement.children) {
    n.parentElement.children = n.parentElement.children.filter(function (c) { return c !== n; });
  }
  this.children.push(_adopt(this, n));
  return n;
};
Node.prototype.addEventListener = function (type, fn) {
  (this.listeners[type] = this.listeners[type] || []).push(fn);
};
/** その要素で起きたことにする（第2引数はイベントの中身。既定は target=自分）。 */
Node.prototype.fire = function (type, event) {
  const self = this;
  (this.listeners[type] || []).forEach(function (fn) {
    fn(Object.assign({ target: self }, event || {}));
  });
};
/** 押したことにする（チェックは、ブラウザと同じようにまず反転させてから change）。 */
Node.prototype.click = function () {
  this.clicked = true;      // 押されたことをテストから見られるように
  if (this.tagName === "input") { this.checked = !this.checked; this.fire("change"); }
  else this.fire("click");
};
Node.prototype.findAll = function (pred) {
  const out = [];
  (function walk(n) {
    if (typeof n === "string" || !n.children) return;
    n.children.forEach(function (c) {
      if (typeof c === "string") return;
      if (pred(c)) out.push(c);
      walk(c);
    });
  })(this);
  return out;
};
Node.prototype.find = function (pred) { return this.findAll(pred)[0] || null; };
Node.prototype.byClass = function (cls) {
  return this.findAll(function (c) {
    return (c.attrs["class"] || "").split(" ").indexOf(cls) >= 0;
  });
};
Node.prototype.byText = function (text) {
  return this.find(function (c) { return c._text === text; });
};
/** その要素より下の文字を、ぜんぶつないで返す（「3点」などの確認に使う）。 */
Node.prototype.text = function () {
  let out = this._text || "";
  this.children.forEach(function (c) {
    out += typeof c === "string" ? c : c.text();
  });
  return out;
};

const NODES = {};
/** その id の要素を用意する（コンポーネントが getElementById で探す先）。 */
function ensureIds(ids) {
  ids.forEach(function (id) {
    if (NODES[id]) return;
    NODES[id] = new Node(id === "comment" ? "textarea" : "div");
    NODES[id].attrs.id = id;
  });
}
ensureIds(["tabs", "panel", "slip"]);
const document = {
  createElement: function (t) { return new Node(t); },
  createTextNode: function (t) { return t; },
  getElementById: function (id) {
    if (NODES[id]) return NODES[id];
    let found = null;
    Object.keys(NODES).forEach(function (key) {
      if (found) return;
      found = NODES[key].find(function (c) { return c.attrs.id === id; });
    });
    return found;
  },
  documentElement: { scrollHeight: 900 },
  addEventListener: function () {},
  querySelector: function (selector) {
    let found = null;
    Object.keys(NODES).forEach(function (key) {
      if (found) return;
      if (NODES[key].matches(selector)) { found = NODES[key]; return; }
      found = NODES[key].find(function (c) { return c.matches(selector); });
    });
    return found;
  },
  querySelectorAll: function (selector) {
    let out = [];
    Object.keys(NODES).forEach(function (key) {
      if (NODES[key].matches(selector)) out.push(NODES[key]);
      out = out.concat(NODES[key].findAll(function (c) { return c.matches(selector); }));
    });
    return out;
  },
};

const SENT = [];       // Streamlitへ返した値（setComponentValue）
const HEIGHTS = [];    // Streamlitへ伝えた高さ
const HANDLERS = [];
const window = {
  addEventListener: function (type, fn) { HANDLERS.push(fn); },
  parent: {
    postMessage: function (m) {
      if (m.type === "streamlit:setComponentValue") SENT.push(m.value);
      if (m.type === "streamlit:setFrameHeight") HEIGHTS.push(m.height);
    },
  },
};

/** Streamlitから render が届いたことにする。 */
function render(payload) {
  HANDLERS.forEach(function (fn) {
    fn({ data: { type: "streamlit:render", args: { payload: payload } } });
  });
}
/**
 * 文字でボタンを探して押す（「クリア」「全通り」「買い目へ追加」など）。
 * ぴったり同じものが無ければ、**その文字で始まる**ボタンを押す
 * （「買い目へ追加（3点）」のように点数が付くことがあるため）。
 */
function press(root, text, nth) {
  let hits = root.findAll(function (c) {
    return c.tagName === "button" && c.text() === text;
  });
  if (!hits.length) {
    hits = root.findAll(function (c) {
      return c.tagName === "button" && c.text().indexOf(text) === 0;
    });
  }
  const button = hits[nth || 0];
  if (!button) throw new Error("ボタンが見つかりません: " + text);
  button.click();
  return button;
}


// --- タイマー（jscには setTimeout が無いので自前で持つ） ---------------------
// テストから `runTimers()` で進める。打ち終わりを待つ処理（コメントの保存など）を
// 待ち時間ゼロで確かめられる。
const TIMERS = [];
function setTimeout(fn, ms) {
  TIMERS.push({ fn: fn, ms: ms || 0, id: TIMERS.length + 1 });
  return TIMERS[TIMERS.length - 1].id;
}
function clearTimeout(id) {
  for (let i = 0; i < TIMERS.length; i++) {
    if (TIMERS[i].id === id) { TIMERS.splice(i, 1); return; }
  }
}
/** たまっているタイマーをぜんぶ動かす（新しく積まれたものは動かさない）。 */
function runTimers() {
  const due = TIMERS.splice(0, TIMERS.length);
  due.forEach(function (t) { t.fn(); });
  return due.length;
}

// --- 親の画面（コンポーネントから window.parent.document で触る先） -------------
// 予想ボードの「馬名から馬柱へ飛ぶ」を確かめるのに使う。
const PARENT_NODES = {};
const parentDocument = {
  getElementById: function (id) { return PARENT_NODES[id] || null; },
  querySelectorAll: function (selector) {
    const out = [];
    Object.keys(PARENT_NODES).forEach(function (id) {
      if (PARENT_NODES[id].matches && PARENT_NODES[id].matches(selector)) out.push(PARENT_NODES[id]);
    });
    return out;
  },
};
/** 親の画面に要素を1つ置く（馬柱の行やタブの見立て）。 */
function putInParent(id, node) {
  node.attrs.id = id;
  PARENT_NODES[id] = node;
  return node;
}
window.parent.document = parentDocument;

