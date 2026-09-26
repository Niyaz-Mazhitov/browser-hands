// Стенд для SETTLE (browser.py) в node: виртуальное время, кадры по 16 мс, фейковые DOM и MutationObserver.
// stdin: {"expression": "<SETTLE + params + )>", "scenario": "<имя>"}; stdout: JSON с итогом успокоения.
// Фейк повторяет то, что делает браузер: attributeFilter, attributeOldValue и типы записей решает observe().
"use strict";

const FRAME = 16;
const LIMIT = 10000;

function run({ expression, scenario }) {
  let now = 0;
  let ids = 1;
  let frameNo = 1;
  const timers = new Map();
  let raf = [];
  let rafEnabled = true;
  const observers = [];

  globalThis.performance = { now: () => now };
  globalThis.setTimeout = (fn, ms) => {
    const id = ids++;
    timers.set(id, { at: now + Math.max(0, ms || 0), fn });
    return id;
  };
  globalThis.clearTimeout = (id) => timers.delete(id);
  globalThis.requestAnimationFrame = (fn) => {
    raf.push(fn);
    return ids++;
  };
  globalThis.innerHeight = 780;
  globalThis.window = globalThis;

  class Element {
    constructor(tag, parent = null, attrs = {}) {
      this.nodeType = 1;
      this.tagName = tag;
      this.parentElement = parent;
      this.attrs = { ...attrs };
    }
    getAttribute(name) {
      return name in this.attrs ? this.attrs[name] : null;
    }
    closest(selector) {
      const tags = selector.split(",").map((s) => s.trim().toUpperCase());
      for (let e = this; e; e = e.parentElement) if (tags.includes(e.tagName)) return e;
      return null;
    }
  }
  const text = (parent) => ({ nodeType: 3, parentElement: parent });

  const body = new Element("BODY");
  const byId = {};
  globalThis.document = {
    body,
    getElementById: (id) => byId[id] || null,
    querySelectorAll: () => [],
  };

  globalThis.MutationObserver = class {
    constructor(callback) {
      this.callback = callback;
      this.options = null;
      this.disconnected = false;
      observers.push(this);
    }
    observe(target, options) {
      this.target = target;
      this.options = options;
      this.lastOptions = options;
    }
    disconnect() {
      this.options = null;
      this.disconnected = true;
    }
  };

  const emit = (records) => {
    for (const o of observers) {
      const opt = o.options;
      if (!opt) continue;
      const seen = records
        .filter((r) => opt[r.type])
        .filter((r) => r.type !== "attributes" || !opt.attributeFilter || opt.attributeFilter.includes(r.attributeName))
        .map((r) => ({ ...r, oldValue: r.type === "attributes" && !opt.attributeOldValue ? null : r.oldValue }));
      if (seen.length) o.callback(seen);
    }
  };
  const setAttr = (el, name, value) => {
    const oldValue = el.getAttribute(name);
    el.attrs[name] = value;
    emit([{ type: "attributes", target: el, attributeName: name, oldValue }]);
  };
  const setText = (node) => emit([{ type: "characterData", target: node, oldValue: "" }]);
  const addChild = (parent) => emit([{ type: "childList", target: parent }]);
  const every = (ms, fn) => {
    const loop = () => {
      fn();
      setTimeout(loop, ms);
    };
    setTimeout(loop, ms);
  };
  const eachFrame = (fn) => {
    const loop = () => {
      fn();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  };
  const at = (ms, fn) => setTimeout(fn, ms);

  const box = new Element("DIV", body, { class: "box" });
  const counter = text(new Element("SPAN", body));
  const combobox = (visibleFrom) => {
    const field = new Element("INPUT", body, { role: "combobox", "aria-controls": "lb" });
    const option = new Element("DIV", body, { role: "option" });
    option.getBoundingClientRect = () =>
      now >= visibleFrom ? { width: 200, height: 20, top: 40, bottom: 60 } : { width: 0, height: 0, top: 0, bottom: 0 };
    option.checkVisibility = () => now >= visibleFrom;
    byId.lb = { querySelectorAll: () => [option] };
    globalThis.__jevFast = { nodes: new Map([[10, field]]) };
  };

  let frame = 0;
  const scenarios = {
    quiet: () => {},
    // Бесконечная анимация: style каждый кадр и счётчик каждые 100 мс.
    animation: () => {
      eachFrame(() => setAttr(box, "style", `transform: translateX(${frame++ % 100}px)`));
      every(100, () => setText(counter));
    },
    "style-only": () => eachFrame(() => setAttr(box, "style", `transform: translateX(${frame++ % 100}px)`)),
    "same-class": () => every(50, () => setAttr(box, "class", "box")),
    "late-change": () => at(120, () => setAttr(box, "class", "box open")),
    "unwatched-attribute": () => every(50, () => setAttr(box, "data-tick", String(now))),
    "script-noise": () => {
      const script = new Element("SCRIPT", body);
      const style = new Element("STYLE", body);
      const template = new Element("TEMPLATE", body);
      every(30, () => {
        setText(text(script));
        setText(text(style));
        addChild(new Element("DIV", template));
      });
    },
    combobox: () => combobox(0),
    "combobox-late": () => {
      combobox(100);
      at(100, () => addChild(body));
    },
    background: () => {
      rafEnabled = false;
    },
    "background-busy": () => {
      rafEnabled = false;
      every(100, () => setText(counter));
    },
    "no-body": () => {
      document.body = null;
      every(100, () => setText(counter));
    },
  };
  if (!scenarios[scenario]) throw new Error(`unknown scenario ${scenario}`);
  scenarios[scenario]();

  let result = null;
  let failure = null;
  (0, eval)(expression).then(
    (value) => {
      result = value;
    },
    (e) => {
      failure = String(e);
    },
  );

  return (async () => {
    await null;
    while (result === null && failure === null && now < LIMIT) {
      while (frameNo * FRAME < now) frameNo++;
      const frameAt = rafEnabled && raf.length ? frameNo * FRAME : Infinity;
      let timerAt = Infinity;
      let timerId = null;
      for (const [id, t] of timers)
        if (t.at < timerAt) {
          timerAt = t.at;
          timerId = id;
        }
      if (frameAt === Infinity && timerAt === Infinity) break;
      if (frameAt <= timerAt) {
        now = frameAt;
        frameNo++;
        const callbacks = raf;
        raf = [];
        for (const cb of callbacks) cb(now);
      } else {
        now = timerAt;
        const t = timers.get(timerId);
        timers.delete(timerId);
        t.fn();
      }
      await null;
      await null;
    }
    const observer = observers[0];
    return {
      result,
      failure,
      now,
      options: observer ? observer.lastOptions || null : null,
      observed: Boolean(observer && observer.target === body),
      disconnected: observers.every((o) => o.disconnected),
      pendingTimers: timers.size,
      pendingFrames: raf.length,
    };
  })();
}

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", async () => {
  const out = await run(JSON.parse(input));
  process.stdout.write(JSON.stringify(out));
});
