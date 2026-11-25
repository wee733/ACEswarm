// Application-facing subset of the Zenoh client. The existing views retain their
// renderers, settings editors and Panel Hub lifecycle; only their transport changes.
async function openGatewaySession(selectors, onDisconnect) {
  const api = '/zenoh/api/zenoh';
  const source = new EventSource(api + '/stream?key_expr=' + encodeURIComponent(selectors));
  const subscriptions = new Set();
  let closed = false;
  let connected = false;

  function end() {
    if (closed) return;
    closed = true;
    source.close();
    for (const sub of subscriptions) sub.undeclare();
  }

  await new Promise((resolve, reject) => {
    const deadline = setTimeout(() => {
      end();
      reject(new Error('等待 Zenoh 网关超时'));
    }, 8000);
    source.onopen = () => {
      clearTimeout(deadline);
      connected = true;
      resolve();
    };
    source.onerror = () => {
      clearTimeout(deadline);
      if (closed) return;
      if (!connected) {
        end();
        reject(new Error('无法打开本机 Zenoh 事件流'));
      } else if (onDisconnect) {
        // Controlpad opens a replacement session. Dashboard lets EventSource reconnect.
        end();
        onDisconnect();
      }
    };
  });

  source.onmessage = event => {
    let frame;
    try { frame = JSON.parse(event.data); } catch { return; }
    if (typeof frame?.key_expr !== 'string' || !frame.payload || typeof frame.payload !== 'object') return;
    const bytes = new TextEncoder().encode(JSON.stringify(frame.payload));
    const sample = {
      keyexpr: () => frame.key_expr,
      payload: () => ({ toBytes: () => bytes }),
      encoding: () => 'application/json',
    };
    for (const sub of subscriptions) {
      if (sub.expr.includes(frame.key_expr)) sub.push(sample);
    }
  };

  async function request(operation, body) {
    if (closed) throw new Error('网关已断开');
    const res = await fetch(api + '/' + operation, {
      method: 'POST', redirect: 'error', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const result = await res.json();
    if (!res.ok || !result.ok) throw new Error(result.message || 'HTTP ' + res.status);
    return result;
  }

  return {
    close: async () => end(),
    async declareSubscriber(expr) {
      const queue = [];
      let waiter;
      let active = true;
      const sub = {
        expr,
        push(value) {
          if (waiter) {
            waiter({ value, done: false });
            waiter = null;
          } else {
            if (queue.length === 64) queue.shift();
            queue.push(value);
          }
        },
        receiver() {
          return {
            [Symbol.asyncIterator]() { return this; },
            next() {
              if (!active || closed) return Promise.resolve({ done: true });
              if (queue.length) return Promise.resolve({ value: queue.shift(), done: false });
              return new Promise(resolve => { waiter = resolve; });
            },
          };
        },
        undeclare() {
          active = false;
          subscriptions.delete(sub);
          queue.length = 0;
          waiter?.({ done: true });
          waiter = null;
        },
      };
      subscriptions.add(sub);
      return sub;
    },
    async get(key, options) {
      const result = await request('get', {
        key_expr: String(key), payload: options.payload || '', timeout_ms: options.timeout?.value ?? 3000,
        consolidation: options.consolidation || 'auto', encoding: options.encoding || 'application/json',
      });
      return result.replies.map(reply => ({
        result: () => {
          let payload;
          try { payload = JSON.parse(reply.payload); } catch { /* Text replies are supported. */ }
          return {
            gatewayError: !reply.ok || payload?.ok === false || payload?.success === false || payload?.accepted === false,
            keyexpr: () => reply.key_expr,
            payload: () => reply.error || reply.payload || '',
          };
        },
      }));
    },
    async put(key, payload, options) {
      await request('put', { key_expr: String(key), payload, encoding: options.encoding || 'application/json',
        priority: options.priority || 'data' });
    },
  };
}
