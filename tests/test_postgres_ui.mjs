import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';

const html = readFileSync(new URL('../onedeploy/static/index.html', import.meta.url), 'utf8');
const cancelStart = html.indexOf('function canCancel(job)');
const cancelEnd = html.indexOf('function show(job)', cancelStart);
assert.ok(cancelStart >= 0 && cancelEnd > cancelStart);
const cancelContext = {};
runInNewContext(html.slice(cancelStart, cancelEnd), cancelContext);

test('running deployment can be cancelled only before its first attempt', () => {
  assert.equal(cancelContext.canCancel({status: 'waiting_input'}), true);
  assert.equal(cancelContext.canCancel({status: 'running', attempts: 0}), true);
  assert.equal(cancelContext.canCancel({status: 'running', attempts: 0, cancel_requested: true}), false);
  assert.equal(cancelContext.canCancel({status: 'running', attempts: 1}), false);
  assert.equal(cancelContext.canCancel({status: 'succeeded', attempts: 0}), false);
});

test('interrupted local deployment shows cleanup only for recorded attempts', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async path => ({ok: true, json: async () => path === '/api/config'
      ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []} : []}),
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  const job = {id: 'a'.repeat(16), mode: 'agent', target: 'local-docker',
    status: 'interrupted', attempts: 1, events: []};
  context.show(job);
  assert.equal(element('retire').hidden, false);
  assert.match(element('retire').textContent, /로컬 시도 정리/);
  context.show({...job, attempts: 0});
  assert.equal(element('retire').hidden, true);
  context.show({...job, target: 'aws-ecs-express'});
  assert.equal(element('retire').hidden, true);
  context.show({...job, attempts: 0, steps: 0, changes: [],
    infrastructure_plan: {target: 'local-docker', planner: 'user', rationale: 'test', resources: []},
    source_digest: 'a'.repeat(64)});
  assert.equal(element('resumeUnstarted').hidden, false);
  context.show({...job, attempts: 0, steps: 1, changes: [],
    infrastructure_plan: {target: 'local-docker', planner: 'user', rationale: 'test', resources: []},
    source_digest: 'a'.repeat(64)});
  assert.equal(element('resumeUnstarted').hidden, true);
  const requests = [];
  context.fetch = async path => {
    requests.push(path);
    const body = path.endsWith('/resume-unstarted') ? {id: job.id, status: 'running'}
      : path === '/api/jobs/' + job.id ? {...job, status: 'waiting_input', attempts: 0, events: []}
      : [];
    return {ok: true, json: async () => body};
  };
  context.show({...job, attempts: 0, steps: 0, changes: [],
    infrastructure_plan: {target: 'local-docker', planner: 'user', rationale: 'test', resources: []},
    source_digest: 'a'.repeat(64)});
  await element('resumeUnstarted').onclick();
  assert.ok(requests.includes('/api/deployments/' + job.id + '/resume-unstarted'));
});
const start = html.indexOf('function postgresUploadHeaders(application)');
const end = html.indexOf("el('deploy').onclick=", start);
assert.ok(start >= 0 && end > start);
const source = html.slice(start, end);

function headers({target = 'aws-ecs-express', publicAccess = true, existing = true,
                  application = 'demo-app'} = {}) {
  const fields = {
    target: {value: target}, public: {checked: publicAccess},
    postgresExisting: {checked: existing},
  };
  const context = {el: id => fields[id], Set, Error};
  runInNewContext(source, context);
  return context.postgresUploadHeaders(application);
}

test('existing RDS opt-in needs no manually entered network headers', () => {
  assert.deepEqual({...headers()}, {
    'X-Postgres-Existing': 'true',
  });
  assert.deepEqual({...headers({existing: false})}, {});
  assert.deepEqual({...headers({target: 'auto'})}, {...headers()});
});

test('database opt-in rejects unsupported target, private service and malformed app ID', () => {
  for (const options of [
    {target: 'cloud-run'}, {publicAccess: false},
    {application: 'demo--app'},
  ]) assert.throws(() => headers(options));
});

test('automatic target keeps existing RDS binding while hiding new-DB creation', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async path => ({ok: true, json: async () => path === '/api/config'
      ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
      : []}),
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('target').value = 'auto';
  element('target').onchange();
  assert.equal(element('postgresOptions').hidden, false);
  assert.equal(element('postgresCreationDetails').hidden, true);
  element('postgresExisting').checked = true;
  element('target').value = 'aws-ecs-express';
  element('target').onchange();
  assert.equal(element('postgresExisting').checked, true);
  assert.equal(element('postgresCreationDetails').hidden, false);
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
  await element('deploy').onclick();
  const upload = requests.find(request => request.path === '/api/deployments');
  assert.ok(upload, element('error').textContent);
  assert.equal(upload.options.headers['X-Postgres-Existing'], 'true');
  assert.equal(upload.options.headers['X-Postgres-Vpc-Id'], undefined);
  assert.equal(upload.options.headers['X-Postgres-Subnet-Ids'], undefined);
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

test('reviewed RDS plan and selected ZIP start one creation and deployment job', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const jobId = 'a'.repeat(16);
  let jobStatus = 'interrupted';
  let creationId = null;
  let operationStatus = 'needs_attention';
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : path.endsWith('/postgres/plan')
          ? {plan_id: 'planned-token-123456789012', account: '123456789012',
             region: 'ap-northeast-2', engine_version: '18.3', instance_class: 'db.t4g.micro',
             storage_type: 'gp3', storage_gib: 20, pricing: {baseline_730h_usd: '20.87'}}
        : path === '/api/deployments' ? {id: jobId, status: 'provisioning'}
        : path.endsWith('/resume-postgres')
          ? (jobStatus = 'waiting_input', {id: jobId, status: 'running'})
        : path.endsWith('/postgres/operation')
          ? {application_id: 'demo-app', status: operationStatus,
             database_id: 'onedeploy-demo-app', vpc_id: 'vpc-12345678',
             subnet_ids: ['subnet-11111111', 'subnet-22222222'],
             message: 'CloudFormation 결과 확인 필요'}
        : {id: jobId, application_id: 'demo-app', status: jobStatus, events: [],
           postgres_creation_id: creationId, attempts: 0, steps: 0,
           target: 'aws-ecs-express', infrastructure_plan: {target: 'aws-ecs-express',
             planner: 'explicit', rationale: 'PostgreSQL app',
             resources: ['new RDS PostgreSQL'],
             database: {binding: 'create', database_id: 'onedeploy-demo-app'}}};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  element('public').checked = true;
  element('postgresPlanVpc').value = 'vpc-12345678';
  element('postgresPlanSubnets').value = 'subnet-11111111,subnet-22222222';
  const file = {name: 'demo-app.zip', size: 100};
  element('file').files = [file];
  await element('postgresPlan').onclick();
  assert.equal(element('postgresCreateDeploy').hidden, false);
  await element('postgresCreateDeploy').onclick();
  const upload = requests.find(request => request.path === '/api/deployments');
  assert.ok(upload);
  assert.equal(upload.options.headers['X-Postgres-Create-Plan'], 'planned-token-123456789012');
  assert.equal(upload.options.body, file);
  assert.equal(requests.some(request => request.path.endsWith('/postgres/create')), false);
  assert.equal(requests.some(request => request.path.endsWith('/postgres/operation')), true);
  assert.equal(element('postgresRecovery').hidden, false);
  assert.equal(element('postgresReconcile').hidden, false);
  assert.equal(element('postgresCreationDetails').open, true);
  element('application').value = 'another-app';
  element('target').value = 'local-docker';
  element('postgresRecovery').hidden = true;
  element('postgresCreationDetails').open = false;
  await context.open(jobId);
  assert.equal(element('application').value, 'demo-app');
  assert.equal(element('target').value, 'aws-ecs-express');
  assert.equal(element('postgresRecovery').hidden, false);
  assert.equal(element('postgresCreationDetails').open, true);
  const operationReads = requests.filter(request => request.path.endsWith('/postgres/operation')).length;
  jobStatus = 'failed';
  await context.open(jobId);
  assert.equal(requests.filter(request => request.path.endsWith('/postgres/operation')).length,
    operationReads);
  assert.match(element('postgresOperationInfo').textContent, /가격 계획을 다시 확인/);
  assert.equal(element('postgresRecovery').hidden, true);
  assert.equal(element('postgresReconcile').hidden, true);
  jobStatus = 'interrupted';
  creationId = 'a'.repeat(16);
  operationStatus = 'succeeded';
  await context.open(jobId);
  assert.equal(element('resumePostgres').hidden, false);
  await element('resumePostgres').onclick();
  assert.equal(requests.some(request => request.path.endsWith('/resume-postgres')),
    true);
});

test('failed RDS creation requires the exact stack ARN before cleanup', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, files: [], hidden: false, disabled: false,
      textContent: '', replaceChildren() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const requests = [];
  const stackId = 'arn:aws:cloudformation:ap-northeast-2:123456789012:stack/onedeploy-db-demo-app/id';
  const context = {
    document: {getElementById: element, hidden: false},
    fetch: async (path, options) => {
      requests.push({path, options});
      const body = path === '/api/config'
        ? {ai_available: true, ai_model: 'test', targets: [], recovery_warnings: []}
        : path === '/api/jobs' ? []
        : path.endsWith('/failed-create/plan')
        ? {plan_id: 'a'.repeat(32), stack_id: stackId, stack_status: 'ROLLBACK_COMPLETE',
           message: '앱 DB 없음'}
        : path.endsWith('/failed-create/start')
        ? {application_id: 'demo-app', status: 'failed_cleaned', message: '정리 완료'}
        : {application_id: 'demo-app', status: 'needs_attention', message: 'ROLLBACK_COMPLETE'};
      return {ok: true, json: async () => body};
    },
    setInterval() {}, setTimeout, FormData, Set, Error, Date,
  };
  runInNewContext(html.split('<script>', 2)[1].split('</script>', 1)[0], context);
  await new Promise(resolve => setImmediate(resolve));
  element('application').value = 'demo-app';
  element('target').value = 'aws-ecs-express';
  await element('postgresOperation').onclick();
  assert.equal(element('postgresRecovery').hidden, false);
  await element('postgresRecoveryPlan').onclick();
  assert.equal(element('postgresRecoveryStart').hidden, false);
  element('postgresRecoveryConfirm').value = stackId + '-wrong';
  await element('postgresRecoveryStart').onclick();
  assert.equal(requests.filter(item => item.path.endsWith('/failed-create/start')).length, 0);
  element('postgresRecoveryConfirm').value = stackId;
  await element('postgresRecoveryStart').onclick();
  const started = requests.find(item => item.path.endsWith('/failed-create/start'));
  assert.equal(JSON.parse(started.options.body).confirm_stack_id, stackId);
  assert.equal(element('postgresRecovery').hidden, true);
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
