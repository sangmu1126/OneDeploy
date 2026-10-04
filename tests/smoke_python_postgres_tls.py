"""Real local PostgreSQL TLS and generated Python image drill; creates no AWS resources."""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from onedeploy.core import ImageBuilder, make_plan
from onedeploy.migrations import collect_sql_migrations


def command(args, *, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} failed: {result.stderr[-3000:]}')
    return result.stdout.strip()


def certificate(directory: Path, name: str, common_name: str, *, ca: Path | None = None):
    key, csr, cert = (directory / f'{name}.{suffix}' for suffix in ('key', 'csr', 'crt'))
    if ca is None:
        command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                 '-keyout', str(key), '-out', str(cert), '-days', '1',
                 '-subj', f'/CN={common_name}', '-addext', 'basicConstraints=critical,CA:TRUE'])
    else:
        command(['openssl', 'req', '-newkey', 'rsa:2048', '-nodes',
                 '-keyout', str(key), '-out', str(csr), '-subj', f'/CN={common_name}'])
        extensions = directory / 'server.ext'
        extensions.write_text('subjectAltName=DNS:db\nextendedKeyUsage=serverAuth\n')
        command(['openssl', 'x509', '-req', '-in', str(csr), '-CA', str(ca / 'ca.crt'),
                 '-CAkey', str(ca / 'ca.key'), '-CAcreateserial', '-out', str(cert),
                 '-days', '1', '-extfile', str(extensions)])
    return cert


def http(base: str, path: str, method='GET', key=None):
    headers = {'X-Probe-Key': key} if key else {}
    request = urllib.request.Request(base + path, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def wait_for_http(base: str):
    for _ in range(120):
        try:
            if http(base, '/health') == (200, {'ok': True}):
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise AssertionError('Python HTTP server did not become ready')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-image', default='postgres:17',
                        help='Existing/pullable PostgreSQL 17 image for the temporary TLS server')
    args = parser.parse_args(argv)
    run_id = uuid.uuid4().hex[:12]
    network = f'onedeploy-tls-{run_id}'
    database = f'onedeploy-tls-db-{run_id}'
    image_db = f'onedeploy/tls-db-{run_id}:local'
    image_app = f'onedeploy/tls-app-{run_id}:local'
    containers = []
    with tempfile.TemporaryDirectory(prefix='onedeploy-python-tls-') as directory:
        root = Path(directory)
        try:
            certificate(root, 'ca', 'OneDeploy local test CA')
            certificate(root, 'server', 'db', ca=root)
            certificate(root, 'wrong-ca', 'OneDeploy wrong test CA')
            db_context = root / 'database'
            db_context.mkdir()
            shutil.copyfile(root / 'server.crt', db_context / 'server.crt')
            shutil.copyfile(root / 'server.key', db_context / 'server.key')
            shutil.copyfile(Path('examples/postgres-probe-python/migrations/9000_onedeploy_probe.sql'),
                            db_context / '001_probe.sql')
            (db_context / 'Dockerfile').write_text(
                'ARG BASE_IMAGE=postgres:17\nFROM ${BASE_IMAGE}\n'
                'COPY --chown=postgres:postgres server.crt server.key /etc/postgresql/certs/\n'
                'RUN chmod 600 /etc/postgresql/certs/server.key\n'
                'COPY 001_probe.sql /docker-entrypoint-initdb.d/\n'
                'CMD ["postgres", "-c", "ssl=on", "-c", '
                '"ssl_cert_file=/etc/postgresql/certs/server.crt", "-c", '
                '"ssl_key_file=/etc/postgresql/certs/server.key"]\n')
            command(['docker', 'build', '--build-arg', f'BASE_IMAGE={args.postgres_image}',
                     '-t', image_db, str(db_context)])
            command(['docker', 'network', 'create', network])
            password = secrets.token_urlsafe(24)
            containers.append(database)
            command(['docker', 'run', '-d', '--name', database, '--network', network,
                     '--network-alias', 'db', '--network-alias', 'wrong-db',
                     '-e', f'POSTGRES_PASSWORD={password}', image_db])
            for _ in range(120):
                ready = subprocess.run(['docker', 'exec', database, 'pg_isready', '-U', 'postgres',
                                        '-d', 'postgres'], capture_output=True, timeout=10)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise AssertionError('PostgreSQL did not become ready')
            assert command(['docker', 'exec', database, 'psql', '-U', 'postgres', '-Atc',
                            'SHOW ssl']) == 'on'

            project = root / 'app'
            shutil.copytree('examples/postgres-probe-python', project)
            shutil.copyfile(root / 'wrong-ca.crt', project / '.onedeploy-wrong-ca.pem')
            plan = make_plan(project, 'wsgi:app.py', None, 4321, '/health',
                             required_env=['PROBE_KEY'])
            assert plan.runtime == 'python-wsgi'
            assert collect_sql_migrations(project).migrations
            ImageBuilder(command, lambda *_: None).build(
                project, plan, image_app, extra_ca_bundle=root / 'ca.crt')
            probe_key = secrets.token_urlsafe(32)
            common_env = ['-e', 'PORT=4321', '-e', 'PGPORT=5432', '-e', 'PGDATABASE=postgres',
                          '-e', 'PGUSER=postgres', '-e', f'PGPASSWORD={password}',
                          '-e', 'PGSSLMODE=verify-full', '-e', f'PROBE_KEY={probe_key}']

            def start_app(label, host, ca_override=None):
                name = f'onedeploy-tls-{label}-{run_id}'
                extra = ['-e', f'PGSSLROOTCERT={ca_override}'] if ca_override else []
                containers.append(name)
                command(['docker', 'run', '-d', '--name', name, '--network', network,
                         '-p', '127.0.0.1::4321', *common_env, '-e', f'PGHOST={host}',
                         *extra, image_app])
                port = command(['docker', 'port', name, '4321/tcp']).rsplit(':', 1)[1]
                base = f'http://127.0.0.1:{port}'
                wait_for_http(base)
                return base, name

            def assert_tls_failure(name, expected):
                result = subprocess.run(['docker', 'exec', name, 'python', '-c',
                                         'import psycopg; psycopg.connect(connect_timeout=5)'],
                                        capture_output=True, text=True, timeout=20)
                assert result.returncode != 0 and expected in result.stderr.lower()

            good, _ = start_app('good', 'db')
            record_id = uuid.uuid4().hex
            path = '/records/' + record_id
            assert http(good, path, 'POST', probe_key) == (200, {'value': record_id})
            assert http(good, path, 'GET', probe_key) == (200, {'value': record_id})
            assert http(good, path, 'DELETE', probe_key) == (200, {'deleted': True})
            wrong_host, wrong_host_name = start_app('wrong-host', 'wrong-db')
            assert http(wrong_host, path, 'GET', probe_key) == (503, {'error': 'database unavailable'})
            assert_tls_failure(wrong_host_name, 'does not match host name')
            wrong_ca, wrong_ca_name = start_app('wrong-ca', 'db', '/app/.onedeploy-wrong-ca.pem')
            assert http(wrong_ca, path, 'GET', probe_key) == (503, {'error': 'database unavailable'})
            assert_tls_failure(wrong_ca_name, 'certificate verify failed')
            print('PASS: Python WSGI image connected to TLS PostgreSQL with verify-full')
            print('PASS: write/read/delete and wrong host/CA rejection')
        finally:
            for name in reversed(containers):
                command(['docker', 'rm', '-f', name], check=False)
            command(['docker', 'network', 'rm', network], check=False)
            for image in (image_app, image_db):
                command(['docker', 'image', 'rm', image], check=False)


if __name__ == '__main__':
    main()
