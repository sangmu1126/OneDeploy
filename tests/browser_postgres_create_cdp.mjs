// Exercise the real network -> PostgreSQL creation controls in headless Chrome.
import assert from 'node:assert/strict';

const [serverUrl, debuggingPort, application, stage = 'create'] = process.argv.slice(2);
assert.match(application, /^dbdrill-[a-f0-9]{8}$/);
assert.ok(['create', 'verify'].includes(stage));
const tabs = await (await fetch(`http://127.0.0.1:${debuggingPort}/json`)).json();
const tab = tabs.find(item => item.type === 'page');
assert.ok(tab?.webSocketDebuggerUrl);
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
  response.error ? waiter.reject(Error(response.error.message)) : waiter.resolve(response.result);
});
socket.addEventListener('close', () => {
  for (const waiter of pending.values()) waiter.reject(Error('Chrome debugging socket closed'));
  pending.clear();
});
function command(method, params = {}) {
  const id = ++serial;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(Error(`Chrome ${method} did not answer within 30 seconds`));
    }, 30000);
    pending.set(id, {
      resolve: value => { clearTimeout(timer); resolve(value); },
      reject: error => { clearTimeout(timer); reject(error); },
    });
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const response = await command('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true,
  });
  if (response.exceptionDetails) throw Error(response.exceptionDetails.text);
  return response.result.value;
}
async function until(check, timeoutMs, intervalMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  throw Error('Browser PostgreSQL creation step timed out');
}
try {
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', {url: serverUrl});
  await until(() => evaluate("document.readyState === 'complete' && document.getElementById('setup').textContent.startsWith('AI 연결 설정됨')"), 30000);
  if (stage === 'verify') {
    await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('postgresOperation').click();return true;})()`);
    const completed = await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('postgresOperationInfo').textContent;if(text.includes(' · needs_attention · '))throw Error(text);return text.includes(' · succeeded · ')&&e('postgresExisting').checked&&e('postgresVpc').value&&e('postgresSubnets').value.split(',').length>=2;})()"), 60000);
    assert.ok(completed);
    console.log('PASS: browser loaded completed RDS operation and filled existing-DB inputs');
  } else {
  await evaluate(`(() => {const e=id=>document.getElementById(id);e('application').value=${JSON.stringify(application)};e('target').value='aws-ecs-express';e('target').onchange();e('networkDiscover').click();return true;})()`);
  await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('networkDiscoverInfo').textContent;if(text&&!text.includes('서브넷을 채웠습니다'))throw Error(text);return text.includes('서브넷을 채웠습니다')&&e('networkVpc').value&&e('postgresPlanVpc').value===e('networkVpc').value&&e('postgresPlanSubnets').value.split(',').length>=2;})()"), 60000);
  await evaluate("document.getElementById('networkPlan').click();true");
  await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('networkPlanInfo').textContent;if(text&&!text.includes('생성하지 않았습니다'))throw Error(text);return text.includes('생성하지 않았습니다')&&!e('networkCreate').hidden;})()"), 60000);
  console.log('PASS: browser reviewed the read-only network plan');
  await evaluate("document.getElementById('networkCreate').click();true");
  await until(() => evaluate("(() => {const text=document.getElementById('networkOperationInfo').textContent;if(text.includes('needs_attention'))throw Error(text);return text.includes(' · succeeded · ');})()"), 15 * 60 * 1000);
  console.log('PASS: browser created the app-owned network');
  await evaluate("document.getElementById('postgresPlan').click();true");
  await until(() => evaluate("(() => {const e=id=>document.getElementById(id);const text=e('postgresPlanInfo').textContent;if(text&&!text.includes('이 조회는 DB를 생성하지 않았습니다'))throw Error(text);return text.includes('이 조회는 DB를 생성하지 않았습니다')&&text.includes('730시간')&&!e('postgresCreate').hidden;})()"), 120000);
  console.log('PASS: browser reviewed the read-only RDS plan and price');
  await evaluate("document.getElementById('postgresCreate').click();true");
  await until(() => evaluate("document.getElementById('postgresOperationInfo').textContent.includes(' · running · ')"), 120000);
  console.log('PASS: browser submitted the RDS create operation');
  }
} finally {
  socket.close();
}
