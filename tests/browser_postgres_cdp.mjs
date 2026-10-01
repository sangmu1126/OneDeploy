// Drive the served OneDeploy UI in real Chrome; no third-party browser package required.
import assert from 'node:assert/strict';

const [serverUrl, debuggingPort, archive, mode = 'apply', snapshotName = 'browser-read-only'] = process.argv.slice(2);
const probeKey = process.env.ONEDEPLOY_BROWSER_PROBE_KEY;
assert.ok(serverUrl && debuggingPort && archive && ['apply', 'read-only', 'snapshot-apply'].includes(mode));
if (mode === 'apply') assert.ok(probeKey);
const tabs = await (await fetch(`http://127.0.0.1:${debuggingPort}/json`)).json();
const tab = tabs.find(item => item.type === 'page');
assert.ok(tab?.webSocketDebuggerUrl, 'Chrome has no debuggable page');
const socket = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, {once: true});
  socket.addEventListener('error', reject, {once: true});
});
let serial = 0;
const pending = new Map();
socket.addEventListener('message', event => {
  const response = JSON.parse(event.data);
  const waiter = pending.get(response.id);
  if (!waiter) return;
  pending.delete(response.id);
  if (response.error) waiter.reject(Error(response.error.message));
  else waiter.resolve(response.result);
});
function command(method, params = {}) {
  const id = ++serial;
  return new Promise((resolve, reject) => {
    pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const result = await command('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true,
  });
  if (result.exceptionDetails) throw Error(result.exceptionDetails.text);
  return result.result.value;
}
async function until(check, timeoutMs, intervalMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  throw Error('Browser step timed out');
}
try {
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', {url: serverUrl});
  await until(() => evaluate("document.readyState === 'complete' && document.getElementById('deploy') && document.getElementById('setup').textContent.startsWith('AI 연결 설정됨')"), 30000);
  await evaluate("(() => {const e = id => document.getElementById(id); e('target').value = 'aws-ecs-express'; e('target').onchange(); e('networkDiscover').click();})()");
  const discovered = await until(() => evaluate("(() => {const e = id => document.getElementById(id); const text = e('networkDiscoverInfo').textContent; if (text && !text.includes('서브넷을 채웠습니다')) throw Error(text); return text.includes('서브넷을 채웠습니다') && e('networkVpc').value && e('postgresPlanVpc').value === e('networkVpc').value && e('postgresPlanSubnets').value.split(',').length >= 2;})()"), 60000);
  assert.ok(discovered);
  console.log('PASS: browser filled default VPC and subnets from AWS');
  await evaluate("(() => {const e = id => document.getElementById(id); e('application').value = 'demo-app'; e('target').value = 'aws-ecs-express'; e('target').onchange(); e('public').checked = true; e('postgresExisting').checked = true; e('postgresExisting').onchange(); e('postgresLookup').click();})()");
  const lookup = await until(() => evaluate("(() => {const e = id => document.getElementById(id); const text = e('postgresLookupInfo').textContent; if (text && !text.includes('검증 완료')) throw Error(text); return text.includes('검증 완료') && e('postgresVpc').value && e('postgresSubnets').value && e('postgresVpc').value === e('networkVpc').value;})()"), 60000);
  assert.ok(lookup);
  console.log('PASS: browser RDS lookup filled the verified network');
  if (mode === 'read-only' || mode === 'snapshot-apply') {
    await evaluate("document.getElementById('postgresBackups').click(); true");
    const backup = await until(() => evaluate("(() => {const text = document.getElementById('postgresBackupInfo').textContent; if (text && !text.includes('조회만 수행했습니다')) throw Error(text); return text.includes('자동 백업 보존') && text.includes('수동 스냅샷');})()"), 60000);
    assert.ok(backup);
    console.log('PASS: browser displayed the RDS backup and protection status');
    await evaluate(`(() => {const e = id => document.getElementById(id); e('snapshotName').value = ${JSON.stringify(snapshotName)}; e('snapshotPlan').click(); return true;})()`);
    const planned = await until(() => evaluate(`(() => {const e = id => document.getElementById(id); const text = e('snapshotPlanInfo').textContent; if (text && !text.includes('이 조회는 스냅샷을 생성하지 않았습니다')) throw Error(text); return text.includes(${JSON.stringify('onedeploy-demo-app-' + snapshotName)}) && text.includes('저장 비용') && !e('snapshotCreate').hidden;})()`), 60000);
    assert.ok(planned);
    console.log('PASS: browser showed a read-only snapshot plan and separate create action');
    if (mode === 'snapshot-apply') {
      await evaluate("document.getElementById('snapshotCreate').click(); true");
      await until(() => evaluate("document.getElementById('snapshotOperationInfo').textContent.includes(' · running · ')"), 60000);
      console.log('PASS: browser submitted the planned snapshot creation');
      const completed = await until(async () => {
        const state = await evaluate("(() => {const e = id => document.getElementById(id); return {text: e('snapshotOperationInfo').textContent, reconcile: !e('snapshotReconcile').hidden};})()");
        if (state.text.includes(' · succeeded · ')) return true;
        if (state.text.includes(' · needs_attention · ')) throw Error(state.text);
        if (state.reconcile) await evaluate("document.getElementById('snapshotReconcile').click(); true");
        else await evaluate("document.getElementById('snapshotOperation').click(); true");
        return false;
      }, 15 * 60 * 1000, 5000);
      assert.ok(completed);
      console.log('PASS: browser reconciled an available owned snapshot');
    }
  }
  if (mode === 'apply') {
  const document = await command('DOM.getDocument');
  const input = await command('DOM.querySelector', {nodeId: document.root.nodeId, selector: '#file'});
  assert.ok(input.nodeId);
  await command('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [archive]});
  await evaluate("document.getElementById('deploy').click(); true");
  const jobId = await until(() => evaluate("document.getElementById('jobId').textContent.match(/작업 ([a-f0-9]{16})/)?.[1] || ''"), 120000);
  console.log('Browser deployment job:', jobId);
  const deadline = Date.now() + 35 * 60 * 1000;
  let resumed = false;
  while (Date.now() < deadline) {
    const state = await evaluate("(() => {const e = id => document.getElementById(id); return {status: e('status').textContent, error: e('error').textContent, waiting: !e('inputSection').hidden, resumeReady: !e('resume').disabled, names: [...e('envInputs').querySelectorAll('input')].map(x => x.dataset.name)};})()");
    if (state.error) throw Error(state.error);
    if (state.status === '배포 완료') {
      assert.ok(resumed, 'Environment prompt was not resumed in the browser');
      console.log('PASS: browser upload, environment resume, and AWS deployment completed');
      process.exitCode = 0;
      break;
    }
    if (state.status.includes('실패') || state.status.includes('중단')) throw Error(state.status);
    if (state.waiting && state.resumeReady && !resumed) {
      assert.deepEqual(state.names, ['PROBE_KEY']);
      await evaluate(`(() => {const input = document.querySelector('#envInputs input'); input.value = ${JSON.stringify(probeKey)}; document.getElementById('resume').click(); return true;})()`);
      resumed = true;
      console.log('PASS: browser supplied the requested environment value');
    }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  if (Date.now() >= deadline) throw Error('Browser deployment exceeded 35 minutes');
  }
} finally {
  socket.close();
}
