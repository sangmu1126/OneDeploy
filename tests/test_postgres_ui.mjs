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

test('app network creation needs a reviewed plan and records the selected VPC', async () => {
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
        : path === '/api/aws/default-network'
        ? {account: '123456789012', region: 'ap-northeast-2', vpc_id: 'vpc-12345678',
           subnet_ids: ['subnet-11111111', 'subnet-22222222'],
           availability_zones: ['ap-northeast-2a', 'ap-northeast-2c']}
        : path.endsWith('/network/plan')
        ? {plan_id: 'a'.repeat(32), account: '123456789012', region: 'ap-northeast-2',
           stack_name: 'onedeploy-network-demo-app'}
        : path === '/api/jobs' ? []
        : {application_id: 'demo-app', vpc_id: 'vpc-12345678', status: 'succeeded',
           message: '완료', service_security_group: 'sg-33333333'};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  const script = html.split('<script>', 2)[1].split('</script>', 1)[0];
  runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('target').onchange();
  await element('networkDiscover').onclick();
  assert.equal(element('networkVpc').value, 'vpc-12345678');
  assert.equal(element('postgresPlanVpc').value, 'vpc-12345678');
  assert.equal(element('postgresPlanSubnets').value,
    'subnet-11111111,subnet-22222222');
  assert.equal(requests.filter(item => item.path.endsWith('/network/create')).length, 0);
  await element('networkPlan').onclick();
  assert.equal(element('networkCreate').hidden, false);
  assert.equal(requests.filter(item => item.path.endsWith('/network/create')).length, 0);
  element('networkVpc').value = 'vpc-87654321';
  await element('networkCreate').onclick();
  assert.equal(requests.filter(item => item.path.endsWith('/network/create')).length, 0);
  element('networkVpc').value = 'vpc-12345678';
  await element('networkCreate').onclick();
  const create = requests.find(item => item.path.endsWith('/network/create'));
  assert.equal(JSON.parse(create.options.body).plan_id, 'a'.repeat(32));
  assert.equal(element('postgresPlanVpc').value, 'vpc-12345678');
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
        : path.endsWith('/postgres/backups')
        ? {database_id: 'onedeploy-demo-app', backup_retention_days: 7,
           latest_restorable_time: '2026-10-01T00:00:00Z', deletion_protection: true,
           manual_snapshot_count: 0, manual_snapshots: []}
        : path.endsWith('/snapshots/plan')
        ? {plan_id: 'a'.repeat(32), account: '123456789012', region: 'ap-northeast-2',
           database_id: 'onedeploy-demo-app', snapshot_id: 'onedeploy-demo-app-before-migration',
           manual_snapshot_count: 0, storage_cost_warning: '저장 비용이 발생할 수 있습니다.'}
        : path.endsWith('/snapshots/create')
        ? {application_id: 'demo-app', snapshot_id: 'onedeploy-demo-app-before-migration',
           status: 'running', message: '생성 요청 중'}
        : path.endsWith('/snapshots/onedeploy-demo-app-before-migration/operation')
        ? {application_id: 'demo-app', snapshot_id: 'onedeploy-demo-app-before-migration',
           status: 'pending', message: '생성 중'}
        : path.endsWith('/snapshots/onedeploy-demo-app-before-migration/reconcile')
        ? {application_id: 'demo-app', snapshot_id: 'onedeploy-demo-app-before-migration',
           status: 'succeeded', message: '사용 가능'}
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
  await element('postgresBackups').onclick();
  assert.ok(requests.some(request => request.path === '/api/applications/demo-app/postgres/backups'));
  assert.match(element('postgresBackupInfo').textContent, /자동 백업 보존 7일/);
  assert.match(element('postgresBackupInfo').textContent, /수동 스냅샷 0개/);
  element('snapshotName').value = 'before-migration';
  await element('snapshotPlan').onclick();
  assert.ok(requests.some(request => request.path === '/api/applications/demo-app/snapshots/plan'));
  assert.equal(element('snapshotCreate').hidden, false);
  await element('snapshotCreate').onclick();
  const create = requests.find(request => request.path === '/api/applications/demo-app/snapshots/create');
  assert.equal(JSON.parse(create.options.body).plan_id, 'a'.repeat(32));
  await element('snapshotOperation').onclick();
  assert.equal(element('snapshotReconcile').hidden, false);
  await element('snapshotReconcile').onclick();
  assert.match(element('snapshotOperationInfo').textContent, /succeeded/);
});

test('new RDS plan button shows a read-only capacity quote', async () => {
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
        : {plan_id: 'planned-token-123456789012', account: '123456789012', region: 'ap-northeast-2', engine_version: '18.3',
           instance_class: 'db.t4g.micro', storage_type: 'gp3', storage_gib: 20,
           pricing: {baseline_730h_usd: '20.87'}};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('postgresPlanVpc').value = 'vpc-12345678';
  element('postgresPlanSubnets').value = 'subnet-11111111,subnet-22222222';
  await element('postgresPlan').onclick();
  const plan = requests.find(request => request.path === '/api/applications/demo-app/postgres/plan');
  assert.ok(plan);
  assert.equal(plan.options.method, 'POST');
  assert.equal(JSON.parse(plan.options.body).subnet_ids.length, 2);
  assert.match(element('postgresPlanInfo').textContent, /20\.87 USD/);
  assert.match(element('postgresPlanInfo').textContent, /생성하지 않았습니다/);
});

test('reviewed RDS plan can start creation and fill the resulting DB connection', async () => {
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
        : path.endsWith('/plan')
          ? {plan_id: 'planned-token-123456789012', account: '123456789012',
             region: 'ap-northeast-2', engine_version: '18.3', instance_class: 'db.t4g.micro',
             storage_type: 'gp3', storage_gib: 20, pricing: {baseline_730h_usd: '20.87'}}
          : {application_id: 'demo-app', database_id: 'onedeploy-demo-app',
             status: 'succeeded', message: 'created', vpc_id: 'vpc-12345678',
             subnet_ids: ['subnet-11111111', 'subnet-22222222']};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('postgresPlanVpc').value = 'vpc-12345678';
  element('postgresPlanSubnets').value = 'subnet-11111111,subnet-22222222';
  await element('postgresPlan').onclick();
  assert.equal(element('postgresCreate').hidden, false);
  await element('postgresCreate').onclick();
  const create = requests.find(request => request.path === '/api/applications/demo-app/postgres/create');
  assert.ok(create);
  assert.equal(JSON.parse(create.options.body).plan_id, 'planned-token-123456789012');
  assert.equal(element('postgresExisting').checked, true);
  assert.equal(element('postgresVpc').value, 'vpc-12345678');
  assert.equal(element('postgresSubnets').value, 'subnet-11111111,subnet-22222222');
});

test('RDS retirement requires reviewed plan and exact database ID', async () => {
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
        : path.endsWith('/retirement/plan')
          ? {plan_id: 'a'.repeat(32), application_id: 'demo-app',
             account: '123456789012', region: 'ap-northeast-2',
             database_id: 'onedeploy-demo-app'}
          : {application_id: 'demo-app', database_id: 'onedeploy-demo-app',
             snapshot_id: 'onedeploy-demo-app-final-abcdef123456',
             status: 'succeeded', stage: 'stack_deleted', message: 'done'};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('target').onchange();
  await element('retirementStart').onclick();
  assert.equal(requests.filter(item => item.path.endsWith('/retirement/start')).length, 0);
  await element('retirementPlan').onclick();
  assert.equal(element('retirementStart').hidden, false);
  element('retirementConfirm').value = 'wrong';
  await element('retirementStart').onclick();
  assert.equal(requests.filter(item => item.path.endsWith('/retirement/start')).length, 0);
  element('retirementConfirm').value = 'onedeploy-demo-app';
  await element('retirementStart').onclick();
  const start = requests.find(item => item.path.endsWith('/retirement/start'));
  assert.equal(JSON.parse(start.options.body).confirm_database_id, 'onedeploy-demo-app');
  assert.match(element('retirementOperationInfo').textContent, /final-abcdef123456/);
});
