// Unit tests for the shared browser transport. All messages stay in process.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function transportFixture() {
  const sources = [];
  const requests = [];
  let replies = [];
  class MockEventSource {
    constructor(url) {
      this.url = url;
      sources.push(this);
      queueMicrotask(() => this.onopen());
    }
    close() { this.closed = true; }
    emit(key, payload) { this.onmessage({ data: JSON.stringify({ key_expr: key, payload }) }); }
  }
  async function fetch(url, options) {
    requests.push([url, JSON.parse(options.body)]);
    return { ok: true, json: async () => ({ ok: true, replies }) };
  }
  const script = fs.readFileSync(require.resolve('../groundstation/web/gateway-session.js'), 'utf8');
  const open = vm.runInNewContext(script + '\nopenGatewaySession', {
    EventSource: MockEventSource, fetch, TextEncoder, setTimeout, clearTimeout,
  });
  return { open, sources, requests, setReplies: value => { replies = value; } };
}

test('one event stream fans out JSON samples and releases subscribers on reconnect', async () => {
  const fixture = transportFixture();
  let disconnected = false;
  const session = await fixture.open('*/heartbeat,*/telemetry/**', () => { disconnected = true; });
  const [source] = fixture.sources;
  assert.equal(fixture.sources.length, 1);
  assert.equal(decodeURIComponent(source.url), '/zenoh/api/zenoh/stream?key_expr=*/heartbeat,*/telemetry/**');
  const pose = await session.declareSubscriber({ includes: key => key.endsWith('/local_pos') });
  const heartbeat = await session.declareSubscriber({ includes: key => key.endsWith('/heartbeat') });
  source.emit('test/telemetry/local_pos', { data: { frame_id: 'enu' } });
  const { value } = await pose.receiver().next();
  assert.equal(value.keyexpr(), 'test/telemetry/local_pos');
  assert.equal(value.encoding(), 'application/json');
  assert.deepEqual(JSON.parse(new TextDecoder().decode(value.payload().toBytes())), { data: { frame_id: 'enu' } });
  const waiting = heartbeat.receiver().next();
  source.onerror();
  assert.ok(disconnected && source.closed);
  assert.equal((await waiting).done, true);
  await session.close();
});

test('an empty reply stays empty and rejected operations retain their payload', async () => {
  const fixture = transportFixture();
  const session = await fixture.open('*/heartbeat');
  const options = { timeout: { value: 1200 }, payload: '{}', consolidation: 'latest' };
  assert.equal((await session.get('fixture/status', options)).length, 0);
  assert.deepEqual(fixture.requests[0], ['/zenoh/api/zenoh/get', {
    key_expr: 'fixture/status', payload: '{}', timeout_ms: 1200, consolidation: 'latest', encoding: 'application/json',
  }]);
  fixture.setReplies([{ ok: true, key_expr: 'fixture/status', payload: '{"accepted":false}' }]);
  const [reply] = await session.get('fixture/status', options);
  assert.equal(reply.result().gatewayError, true);
  assert.equal(reply.result().payload(), '{"accepted":false}');
  await session.close();
  await assert.rejects(() => session.get('fixture/status', options), /断开/);
});

test('the saved uXRCE panel acquires explicit arm confirmation and external mode controls', () => {
  const source = fs.readFileSync(require.resolve('../groundstation/web/controlpad-layout.js'), 'utf8');
  const migrate = vm.runInNewContext(source + '\nupdateControlpadLayout');
  const arm = {id: 'arm', keySuffix: '/control/arm', requestType: 'GET', payloadTemplate: '{}',
    gridConfig: {x: 4, y: 5, w: 6, h: 3}};
  const mode = {id: 'set_mode', keySuffix: '/control/set_mode', requestType: 'GET', type: 'select-button',
    selectOptions: [{label: 'Hold', value: 'AUTO.LOITER'}], gridConfig: {x: 3, y: 12, w: 5, h: 4}};
  const saved = {id: 'uxrce', widgets: [arm, mode]};
  const register = {id: 'register_extra', keySuffix: '/control/register_extra', gridConfig: {x: 0, y: 9, w: 8, h: 3}};
  const defaults = {id: 'uxrce', widgets: [
    {...arm, payloadTemplate: '{"value":true,"confirm":true}'},
    {...mode, selectOptions: [{label: 'Extra', value: 'EXTRA'}]}, register,
  ]};
  const result = migrate(migrate(saved, defaults), defaults);
  assert.equal(result.widgets.length, 3);
  assert.equal(result.widgets[0].confirm, true);
  assert.equal(result.widgets[0].payloadTemplate, defaults.widgets[0].payloadTemplate);
  assert.equal(result.widgets[0].gridConfig, arm.gridConfig);
  assert.equal(result.widgets[1].selectOptions.length, 2);
  assert.equal(result.widgets[1].selectOptions[0].label, 'Hold');
  assert.equal(result.widgets[2].gridConfig.y, 16);
});

test('GoTo upgrades station and browser layouts without duplicating or moving existing controls', () => {
  const source = fs.readFileSync(require.resolve('../groundstation/web/controlpad-layout.js'), 'utf8');
  const migrate = vm.runInNewContext(source + '\nupdateControlpadLayout');
  const register = {id: 'register_extra', keySuffix: '/control/register_extra', gridConfig: {x: 0, y: 9, w: 12, h: 3}};
  const goto = {id: 'goto', keySuffix: '/control/goto', gridConfig: {x: 12, y: 9, w: 12, h: 3}};
  const shipped = {id: 'uxrce', cols: 24, widgets: [register, goto]};
  const effective = migrate({...shipped, widgets: [register]}, shipped);
  assert.deepEqual({...effective.widgets[1].gridConfig}, goto.gridConfig);
  const custom = {id: 'custom', gridConfig: {x: 13, y: 10, w: 4, h: 4}};
  const saved = {...effective, widgets: [register, custom]};
  const result = migrate(migrate(saved, effective), effective);
  assert.equal(result.widgets.length, 3);
  assert.equal(result.widgets[1], custom);
  assert.equal(result.widgets[2].gridConfig.y, 14);
  const renamed = {...goto, id: 'my-goto', gridConfig: custom.gridConfig};
  const edited = migrate({...saved, widgets: [register, renamed]}, effective);
  assert.equal(edited.widgets.length, 2);
  assert.equal(edited.widgets[1], renamed);
});
