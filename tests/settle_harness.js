// Стенд для ожиданий READY и CHANGE (browser.py) в node: виртуальное время, кадры по 16 мс, фейковые DOM,
// MutationObserver, анимации, шрифты, readyState, видимость и MessageChannel.
// stdin: {"expression": "<READY|CHANGE + params + )>", "scenario": "<имя>"}; stdout: JSON с итогом и тем, что осталось
// висеть после него (таймеры, кадры, ходы MessageChannel, слушатели).
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
  let tasks = []; // макрозадачи MessageChannel: выполняются в текущий момент, раньше кадров и таймеров
  let taskRuns = 0;
  const observers = [];
  const listeners = new Map();
  const animations = [];
  const elements = [];

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
  globalThis.MessageChannel = class {
    constructor() {
      const port1 = {
        onmessage: null,
        closed: false,
        close() {
          this.closed = true;
        },
      };
      this.port1 = port1;
      this.port2 = {
        postMessage: () => {
          if (!port1.closed) tasks.push(() => port1.onmessage && port1.onmessage({ data: 0 }));
        },
        close() {},
      };
    }
  };
  globalThis.innerHeight = 780;
  globalThis.window = globalThis;

  class Element {
    constructor(tag, parent = null, attrs = {}) {
      this.nodeType = 1;
      this.tagName = tag;
      this.parentElement = parent;
      this.attrs = { ...attrs };
      this.visible = true;
      elements.push(this);
    }
    getAttribute(name) {
      return name in this.attrs ? this.attrs[name] : null;
    }
    closest(selector) {
      const tags = selector.split(",").map((s) => s.trim().toUpperCase());
      for (let e = this; e; e = e.parentElement) if (tags.includes(e.tagName)) return e;
      return null;
    }
    checkVisibility() {
      return this.visible;
    }
  }
  const text = (parent) => ({ nodeType: 3, parentElement: parent });

  let fontsLoaded = () => {};
  const body = new Element("BODY");
  const byId = {};
  globalThis.document = {
    body,
    documentElement: null,
    readyState: "complete",
    visibilityState: "visible",
    fonts: { status: "loaded", ready: Promise.resolve() },
    getElementById: (id) => byId[id] || null,
    // Только селекторы вида [attr="value"] — других READY у document не спрашивает.
    querySelectorAll: (selector) => {
      const m = /^\[([a-z-]+)="([^"]*)"\]$/.exec(selector);
      return m ? elements.filter((e) => e.getAttribute(m[1]) === m[2]) : [];
    },
    getAnimations: () => animations,
    addEventListener: (type, fn) => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener: (type, fn) => listeners.get(type)?.delete(fn),
  };
  const dispatch = (type) => {
    for (const fn of [...(listeners.get(type) || [])]) fn({ type });
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
  // Значимая мутация каждый кадр до `ms` включительно (фреймворк дорисовывает цепочкой кадров), потом тишина.
  const framesUntil = (ms, fn) => {
    const loop = () => {
      if (now > ms) return;
      fn();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  };
  // Анимация Web Animations: идёт с момента создания `duration` мс (Infinity — бесконечная, как спиннер).
  const animate = (duration) => {
    const start = now;
    animations.push({
      get playState() {
        return now < start + duration ? "running" : "finished";
      },
      effect: { getComputedTiming: () => ({ endTime: duration }) },
    });
  };
  const hidden = () => {
    rafEnabled = false;
    document.visibilityState = "hidden";
  };

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
    // Бесконечная JS-анимация: style каждый кадр (не значим) и счётчик каждые 100 мс (значим).
    animation: () => {
      eachFrame(() => setAttr(box, "style", `transform: translateX(${frame++ % 100}px)`));
      every(100, () => setText(counter));
    },
    // Текст меняется каждый кадр: двух тихих кадров не бывает.
    ticker: () => eachFrame(() => setText(counter)),
    "style-only": () => eachFrame(() => setAttr(box, "style", `transform: translateX(${frame++ % 100}px)`)),
    "same-class": () => every(50, () => setAttr(box, "class", "box")),
    "late-change": () => at(80, () => setAttr(box, "class", "box open")), // после двух тихих кадров — не ждём
    "busy-5-frames": () => framesUntil(80, () => setText(counter)), // мутации в кадрах 1–5 (16…80 мс)
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
    "infinite-animation": () => animate(Infinity),
    "finite-animation": () => animate(300),
    "aria-busy": () => {
      const list = new Element("DIV", body, { "aria-busy": "true" });
      at(200, () => setAttr(list, "aria-busy", "false"));
    },
    "aria-busy-hidden": () => {
      const list = new Element("DIV", body, { "aria-busy": "true" });
      list.visible = false;
    },
    fonts: () => {
      document.fonts.status = "loading";
      document.fonts.ready = new Promise((resolve) => (fontsLoaded = resolve));
      at(100, () => {
        document.fonts.status = "loaded";
        fontsLoaded();
      });
    },
    "loading-doc": () => {
      document.readyState = "interactive";
      at(150, () => {
        document.readyState = "complete";
        dispatch("readystatechange");
      });
    },
    "animation-end": () => at(50, () => dispatch("animationend")),
    combobox: () => combobox(0),
    "combobox-late": () => {
      combobox(100);
      framesUntil(100, () => setText(counter)); // поле перерисовывается, пока подсказки не видны
    },
    background: hidden,
    "background-busy": () => {
      hidden();
      every(100, () => setText(counter));
    },
    "background-loading": () => {
      hidden();
      document.readyState = "interactive";
      at(150, () => {
        document.readyState = "complete";
        dispatch("readystatechange");
      });
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
      if (tasks.length) {
        taskRuns++;
        tasks.shift()();
      } else {
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
      pendingTasks: tasks.length,
      taskRuns,
      listeners: [...listeners.values()].reduce((n, set) => n + set.size, 0),
    };
  })();
}

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", async () => {
  const out = await run(JSON.parse(input));
  process.stdout.write(JSON.stringify(out));
});
