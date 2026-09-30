import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';

const html = readFileSync(new URL('../onedeploy/static/index.html', import.meta.url), 'utf8');
const start = html.indexOf('function postgresUploadHeaders(application)');
const end = html.indexOf("el('deploy').onclick=", start);
assert.ok(start >= 0 && end > start);
const source = html.slice(start, end);

function headers({target = 'aws-ecs-express', publicAccess = true, existing = true,
                  vpc = 'vpc-12345678', subnets = 'subnet-11111111,subnet-22222222',
                  application = 'demo-app'} = {}) {
  const fields = {
    target: {value: target}, public: {checked: publicAccess},
    postgresExisting: {checked: existing}, postgresVpc: {value: vpc},
    postgresSubnets: {value: subnets},
  };
  const context = {el: id => fields[id], Set, Error};
  runInNewContext(source, context);
  return context.postgresUploadHeaders(application);
}

test('existing RDS opt-in sends only the server-supported headers', () => {
  assert.deepEqual({...headers()}, {
    'X-Postgres-Existing': 'true',
    'X-Postgres-Vpc-Id': 'vpc-12345678',
    'X-Postgres-Subnet-Ids': 'subnet-11111111,subnet-22222222',
  });
  assert.deepEqual({...headers({existing: false})}, {});
});

test('database opt-in rejects unsupported target, private service and malformed network', () => {
  for (const options of [
    {target: 'auto'}, {target: 'cloud-run'}, {publicAccess: false},
    {vpc: 'vpc-invalid'}, {subnets: 'subnet-11111111,subnet-11111111'},
    {subnets: 'subnet-11111111'}, {application: 'demo--app'},
  ]) assert.throws(() => headers(options));
});

test('deploy button includes existing RDS headers in the upload request', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', dataset: {},
      replaceChildren() {}, querySelectorAll() { return []; },
      append() {}, setAttribute() {},
    });
    return elements.get(id);
  };
  const requests = [];
  const job = {id: '0123456789abcdef', application_id: 'demo-app',
    status: 'failed', target: 'aws-ecs-express', events: [], changes: []};
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/deployments' ? {id: job.id}
        : path === '/api/jobs' ? [] : job;
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  const script = html.split('<script>', 2)[1].split('</script>', 1)[0];
  runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  element('file').files = [{name: 'app.zip'}];
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('public').checked = true;
  element('postgresExisting').checked = true;
  element('postgresVpc').value = 'vpc-12345678';
  element('postgresSubnets').value = 'subnet-11111111,subnet-22222222';
  await element('deploy').onclick();
  const upload = requests.find(request => request.path === '/api/deployments');
  assert.ok(upload, element('error').textContent);
  assert.equal(upload.options.headers['X-Postgres-Existing'], 'true');
  assert.equal(upload.options.headers['X-Postgres-Vpc-Id'], 'vpc-12345678');
  assert.equal(upload.options.headers['X-Postgres-Subnet-Ids'],
    'subnet-11111111,subnet-22222222');
});

test('existing RDS lookup fills the network fields from the authenticated API', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : {database_id: 'onedeploy-demo-app', account: '123456789012',
           region: 'ap-northeast-2', engine_version: '18.3',
           vpc_id: 'vpc-12345678', subnet_ids: ['subnet-11111111', 'subnet-22222222']};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('postgresExisting').checked = true;
  await element('postgresLookup').onclick();
  const lookup = requests.find(request => request.path === '/api/applications/demo-app/postgres');
  assert.ok(lookup);
  assert.equal(lookup.options.headers['X-OneDeploy-Token'], '__TOKEN__');
  assert.equal(element('postgresVpc').value, 'vpc-12345678');
  assert.equal(element('postgresSubnets').value, 'subnet-11111111,subnet-22222222');
});
