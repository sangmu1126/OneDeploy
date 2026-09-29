from __future__ import annotations

import argparse
import difflib
import fcntl
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from onedeploy.analysis import AISettings, analyze_project, redact
from onedeploy.agent import DeploymentAgent, DeploymentTools, NeedsEnvironment, OpenAIDeployAgent
from onedeploy.aws import AwsExpressAdapter, AwsSettings
from onedeploy.cloud import CloudRunAdapter, CloudRunSettings
from onedeploy.core import MAX_UPLOAD, DeploymentPlan, LocalDockerAdapter, extract_project, folder_upload_to_zip, source_digest, validate_environment
from onedeploy.health import check_deployment
from onedeploy.infrastructure import (TARGET_RESOURCES, OpenAIInfrastructurePlanner,
                                      inspect_infrastructure, plan_infrastructure, validate_infrastructure)


def dockerfile_diff(source: Path, plan: dict) -> str:
    previous = (source / "Dockerfile").read_text() if plan.get("dockerfile_source") == "existing" else ""
    return "".join(difflib.unified_diff(previous.splitlines(keepends=True),
                                        plan["dockerfile"].splitlines(keepends=True),
                                        fromfile="Dockerfile (uploaded)" if previous else "/dev/null",
                                        tofile="Dockerfile"))


class StateDirectoryLock:
    """Hold an OS lock for the entire lifetime of one server process."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.file = None

    def __enter__(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.file = (self.root / '.server.lock').open('a+')
        self.file_path = self.root / '.server.lock'
        self.file_path.chmod(0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise RuntimeError(f'이미 다른 OneDeploy 서버가 이 상태 디렉터리를 사용 중입니다: {self.root}') from None
        return self.root

    def __exit__(self, *_):
        if self.file is not None:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None


class App:
    def __init__(self, root: Path, ai_settings: AISettings | None = None, agent_factory=OpenAIDeployAgent,
                 cloud_settings: CloudRunSettings | None = None, aws_settings: AwsSettings | None = None,
                 monitor_interval: int = 300, infrastructure_planner_factory=OpenAIInfrastructurePlanner):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs = {}
        self.health_history = {}
        self.monitor_errors = {}
        if type(monitor_interval) is not int or (monitor_interval != 0 and not 60 <= monitor_interval <= 3600):
            raise ValueError('Monitoring interval must be 0 or 60–3600 seconds')
        self.monitor_interval = monitor_interval
        self.ai_settings = ai_settings if ai_settings is not None else AISettings.from_environment()
        self.agent_factory = agent_factory
        self.infrastructure_planner_factory = infrastructure_planner_factory
        self.cloud_settings = cloud_settings if cloud_settings is not None else CloudRunSettings.from_environment()
        self.aws_settings = aws_settings if aws_settings is not None else AwsSettings.from_environment()
        self.recovery_warnings = []
        self.restore()

    def restore(self):
        for path in sorted(self.root.glob("*/job.json")):
            try:
                job = json.loads(path.read_text())
                if not isinstance(job, dict):
                    raise ValueError("Invalid job record")
                job_id = path.parent.name
                if not re.fullmatch(r"[a-f0-9]{16}", job_id) or job.get("id") != job_id:
                    raise ValueError("Invalid job identity")
                if ("application_id" in job and not re.fullmatch(
                        r"[a-z][a-z0-9-]{2,30}", job["application_id"])):
                    raise ValueError("Invalid application identity")
                project = Path(job["project"]).resolve()
                if not project.is_relative_to((path.parent / "source").resolve()):
                    raise ValueError("Invalid project path")
                if job.get("plan") is not None:
                    DeploymentPlan(**job["plan"])
                elif job.get("mode") != "agent":
                    raise ValueError("Missing deployment plan")
                if job["status"] not in {"planned", "running", "waiting_input", "succeeded", "failed", "interrupted", "cancelled"}:
                    raise ValueError("Invalid job status")
                if (not isinstance(job["events"], list) or any(
                        not isinstance(event, dict) or any(not isinstance(event.get(key), str)
                        for key in ("time", "stage", "message")) for event in job["events"])):
                    raise ValueError("Invalid events")
                job.setdefault("created_at", datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
                if not isinstance(job["created_at"], str):
                    raise ValueError("Invalid timestamp")
                self.jobs[job_id] = job
                health_file = path.parent / 'health.json'
                if health_file.is_file():
                    try:
                        history = json.loads(health_file.read_text())
                        if (not isinstance(history, list) or len(history) > 20
                                or any(not isinstance(item, dict)
                                       or type(item.get('healthy')) is not bool
                                       or not isinstance(item.get('checked_at'), str)
                                       or not isinstance(item.get('reason'), str)
                                       or item.get('source') not in {'automatic', 'manual'}
                                       for item in history)):
                            raise ValueError('Invalid health history')
                        self.health_history[job_id] = history
                    except (OSError, ValueError, TypeError):
                        self.recovery_warnings.append(f"상태 확인 기록을 불러오지 못했습니다: {job_id}")
                if job["status"] == "running":
                    job["status"] = "interrupted"
                    if job.get('aws_update_submitted') and not job.get('aws_update_failed_at'):
                        job['aws_update_failed_at'] = datetime.now(timezone.utc).isoformat()
                    job["events"].append({"time": datetime.now(timezone.utc).isoformat(),
                        "stage": "interrupted", "message": "서버 재시작으로 완료 여부를 확인하지 못했습니다. 컨테이너 상태를 확인하세요. 자동 재배포는 하지 않습니다."})
                    self.save(job_id)
                if job.get('deployment_state') == 'deleting':
                    job['deployment_state'] = 'delete_failed'
                    job['events'].append({"time": datetime.now(timezone.utc).isoformat(),
                        "stage": "retire_interrupted", "message": "서버 재시작으로 종료 확인이 중단됐습니다. 배포 종료를 다시 실행할 수 있습니다."})
                    self.save(job_id)
                if job.get('aws_image_cleanup_state') == 'running':
                    job['aws_image_cleanup_state'] = 'failed'
                    self.save(job_id)
                if job.get('release_rollback_state') == 'running':
                    job['release_rollback_state'] = ('needs_attention' if job.get('release_rollback_submitted') else 'failed')
                    if job.get('release_rollback_submitted'):
                        job['deployment_state'] = 'needs_attention'
                        job.setdefault('release_rollback_failed_at', datetime.now(timezone.utc).isoformat())
                    self.save(job_id)
            except (OSError, ValueError, KeyError, TypeError):
                self.recovery_warnings.append(f"작업 기록을 불러오지 못했습니다: {path.parent.name}")
        for job in self.jobs.values():
            rollback_target = self.jobs.get(job.get('release_rollback_target_id') or job.get('replaces_job_id'))
            legacy_rollback = 'release_rollback_restore_pending' not in job
            if (rollback_target and job.get('release_rollback_state') == 'succeeded'
                    and job.get('deployment_state') == 'superseded'
                    and (job.get('release_rollback_restore_pending') or legacy_rollback)
                    and rollback_target.get('deployment_state') == 'superseded'
                    and not any(successor is not job
                                and successor.get('replaces_job_id') == rollback_target['id']
                                and successor.get('status') == 'succeeded'
                                and successor.get('created_at', '') > job.get('created_at', '')
                                for successor in self.jobs.values())):
                rollback_target['deployment_state'] = 'active'
                rollback_target['result']['images'] = list(dict.fromkeys(
                    [*(rollback_target['result'].get('images') or [rollback_target['result']['image']]),
                     *(job.get('result', {}).get('images') or [])]))
                self.save(rollback_target['id'])
            if job.get('release_rollback_restore_pending') and rollback_target and \
                    rollback_target.get('deployment_state') == 'active':
                job['release_rollback_restore_pending'] = False
                self.save(job['id'])
            previous = self.jobs.get(job.get('replaces_job_id'))
            if not previous or previous.get('deployment_state', 'active') != 'active':
                continue
            if (job.get('status') == 'succeeded' and job.get('result')
                    and job.get('deployment_state', 'active') == 'active'
                    and not job.get('release_rollback_state')):
                previous['deployment_state'] = 'superseded'
                self.save(previous['id'])
            elif ((job.get('aws_update_submitted') or any(event.get('stage') == 'update_submitting'
                      for event in job.get('events', []))) and not job.get('aws_reconciled')
                  and job.get('status') in {'failed', 'interrupted'}):
                previous['deployment_state'] = 'needs_attention'
                self.save(previous['id'])

    def summaries(self):
        with self.lock:
            return [{"id": job["id"], "status": job["status"], "created_at": job.get("created_at"),
                     "application_id": job.get("application_id", job["id"]),
                     "deployment_state": job.get("deployment_state", "active"),
                     "release_rollback_state": job.get('release_rollback_state'),
                     "analyzer": (job.get("plan") or {}).get("analyzer", job.get("mode", "static")),
                     "result": job.get("result"),
                     "last_health": (self.health_history.get(job['id']) or [None])[-1],
                     "monitor_error": self.monitor_errors.get(job['id'])}
                    for job in sorted(self.jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)]

    def releases(self, application_id):
        with self.lock:
            return [{"id": job["id"], "status": job["status"], "target": job.get("target", "local-docker"),
                     "created_at": job.get("created_at"), "result": job.get("result"),
                     "deployment_state": job.get("deployment_state", "active"),
                     "release_rollback_state": job.get('release_rollback_state')}
                    for job in sorted(self.jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)
                    if job.get("application_id", job["id"]) == application_id]

    def ensure_application_available(self, application_id, target):
        # The caller holds self.lock while reserving the new job.
        if any(job.get("application_id") == application_id and job.get("target") == target
               and job.get("status") in {"running", "waiting_input"} for job in self.jobs.values()):
            raise ValueError(f"{application_id}의 {target} 배포가 이미 진행 중입니다.")
        if any(job.get("application_id") == application_id and job.get("target") == target
               and job.get("deployment_state") in {"deleting", "needs_attention"} for job in self.jobs.values()):
            raise ValueError(f"{application_id}의 {target} 서비스 상태를 먼저 확인해야 합니다.")
        if any(job.get('application_id') == application_id and job.get('target') == target
               and job.get('aws_image_cleanup_state') == 'running' for job in self.jobs.values()):
            raise ValueError(f'{application_id}의 실패 이미지 정리가 진행 중입니다.')
        if any(job.get('application_id') == application_id and job.get('target') == target
               and job.get('release_rollback_state') == 'running' for job in self.jobs.values()):
            raise ValueError(f'{application_id}의 이전 릴리스 롤백이 진행 중입니다.')

    def event(self, job_id, stage, message):
        with self.lock:
            job = self.jobs[job_id]
            job["events"].append({"time": datetime.now(timezone.utc).isoformat(),
                                  "stage": stage, "message": message})
            self.save(job_id)

    def check_and_record_health(self, job_id: str, source: str = 'manual') -> dict:
        if source not in {'automatic', 'manual'}:
            raise ValueError('Invalid health check source')
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.get('status') != 'succeeded' or not isinstance(job.get('result'), dict):
                raise ValueError('Only completed deployments can be checked')
            snapshot = json.loads(json.dumps(job))
        result = check_deployment(snapshot)
        entry = {'healthy': bool(result.get('healthy')),
                 'checked_at': result.get('checked_at') or datetime.now(timezone.utc).isoformat(),
                 'reason': redact(str(result.get('reason') or ''))[:300], 'source': source}
        with self.lock:
            current = self.jobs.get(job_id)
            if (not current or current.get('status') != 'succeeded'
                    or current.get('deployment_state', 'active') != 'active'
                    or current.get('result') != snapshot['result']
                    or (source == 'automatic' and any(
                        other is not current
                        and other.get('application_id', other['id']) == current.get('application_id', current['id'])
                        and other.get('target', 'local-docker') == current.get('target', 'local-docker')
                        and other.get('status') in {'running', 'waiting_input'}
                        for other in self.jobs.values()))):
                return result
            history = (self.health_history.get(job_id, []) + [entry])[-20:]
            path = self.root / job_id / 'health.json'
            temporary = None
            try:
                descriptor, temporary = tempfile.mkstemp(prefix='.health-', suffix='.tmp', dir=path.parent)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                    json.dump(history, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                self.health_history[job_id] = history
                self.monitor_errors.pop(job_id, None)
            except OSError as exc:
                self.monitor_errors[job_id] = '상태 확인 기록 저장 실패: ' + str(exc)[:200]
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
        return result

    def monitor_once(self) -> None:
        with self.lock:
            jobs = list(self.jobs.values())
            candidates = [job['id'] for job in jobs
                          if job.get('status') == 'succeeded' and job.get('result')
                          and job.get('deployment_state', 'active') == 'active'
                          and job.get('release_rollback_state') not in {'running', 'needs_attention'}
                          and not any(other is not job
                                      and other.get('application_id', other['id']) == job.get('application_id', job['id'])
                                      and other.get('target', 'local-docker') == job.get('target', 'local-docker')
                                      and other.get('status') in {'running', 'waiting_input'} for other in jobs)]
        for job_id in candidates:
            try:
                self.check_and_record_health(job_id, 'automatic')
            except Exception as exc:
                # Monitoring is observational; it must not alter deployment history on failure.
                with self.lock:
                    self.monitor_errors[job_id] = '자동 상태 확인 실패: ' + str(exc)[:200]
                continue

    def monitor_loop(self, stop: threading.Event) -> None:
        if stop.wait(5):
            return
        while not stop.is_set():
            self.monitor_once()
            if stop.wait(self.monitor_interval):
                return

    def save(self, job_id):
        path = self.root / job_id / "job.json"
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix='.job-', suffix='.tmp', dir=path.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(self.jobs[job_id], output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            job = self.jobs[job_id]
            job['status'] = 'failed'
            job.pop('result', None)
            if not job.get('persistence_failed'):
                job['persistence_failed'] = True
                job.setdefault('events', []).append({
                    'time': datetime.now(timezone.utc).isoformat(), 'stage': 'error',
                    'message': '작업 기록을 저장하지 못했습니다. 배포 리소스 상태를 직접 확인하세요.'})
            raise RuntimeError('배포 작업 기록 저장에 실패했습니다.') from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def run_agent(self, job_id, environment=None):
        environment = {} if environment is None else environment
        tools = None
        def checkpoint(**updates):
            with self.lock:
                job = self.jobs[job_id]
                if "change" in updates:
                    job.setdefault("changes", []).append(updates.pop("change"))
                job.update(updates)
                if "plan" in updates and updates["plan"] is None:
                    job["diff"] = ""
                if job.get("plan"):
                    job["diff"] = dockerfile_diff(Path(job["project"]), job["plan"])
                self.save(job_id)
        try:
            job = self.jobs[job_id]
            target = job.get("target", "local-docker")
            adapter_factory = LocalDockerAdapter
            if target == "cloud-run":
                settings = CloudRunSettings(**job["cloud"])
                adapter_factory = lambda event: CloudRunAdapter(event, settings, public=job.get("public", False))
            elif target == "aws-ecs-express":
                settings = AwsSettings(**job["aws"])
                adapter_factory = lambda event: AwsExpressAdapter(event, settings, existing=job.get('prior_result'),
                                                                    checkpoint=checkpoint)
            tools = DeploymentTools(Path(job["project"]), self.root / job_id / "work", job_id,
                                    environment, lambda s, m: self.event(job_id, s, m), checkpoint,
                                    attempts=job.get("attempts", 0), adapter_factory=adapter_factory, target=target,
                                    infrastructure_plan=job.get('infrastructure_plan'))
            self.event(job_id, "starting", "AI가 작업용 소스에서 배포를 준비합니다.")
            result = DeploymentAgent(self.agent_factory(self.ai_settings), tools,
                                     steps=job.get("steps", 0)).run()
            checkpoint(status="succeeded", result=result, missing_environment=[], deployment_state="active")
            if job.get('replaces_job_id'):
                with self.lock:
                    previous = self.jobs.get(job['replaces_job_id'])
                    if previous and previous.get('deployment_state', 'active') == 'active':
                        if (target == 'aws-ecs-express' and result.get('previous_task_definition_arn')
                                and previous.get('result')):
                            previous['result']['task_definition_arn'] = result['previous_task_definition_arn']
                        previous['deployment_state'] = 'superseded'
                        self.save(previous['id'])
            self.event(job_id, "succeeded", "실제 HTTP 응답 확인 완료")
        except NeedsEnvironment as exc:
            # Previous values are not persisted, so ask for their names again on resume too.
            names = sorted(set(exc.names) | set(environment))
            checkpoint(status="waiting_input", missing_environment=names, input_reason=exc.reason)
            self.event(job_id, "waiting_input", exc.reason)
        except Exception as exc:
            message = str(exc)
            for value in sorted(set(environment.values()), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            self.event(job_id, "error", message)
            checkpoint(status="failed")
            if self.jobs[job_id].get('aws_update_submitted') and not self.jobs[job_id].get('aws_update_failed_at'):
                checkpoint(aws_update_failed_at=datetime.now(timezone.utc).isoformat())
            if (self.jobs[job_id].get('aws_update_submitted') or any(
                    event.get('stage') == 'update_submitting' for event in self.jobs[job_id].get('events', []))) \
                    and self.jobs[job_id].get('replaces_job_id'):
                with self.lock:
                    previous = self.jobs.get(self.jobs[job_id]['replaces_job_id'])
                    if previous and previous.get('deployment_state', 'active') == 'active':
                        previous['deployment_state'] = 'needs_attention'
                        self.save(previous['id'])
        finally:
            environment.clear()
            if tools is not None:
                tools.environment.clear()

    def run(self, job_id, environment=None):
        environment = {} if environment is None else environment
        try:
            job = self.jobs[job_id]
            result = LocalDockerAdapter(lambda s, m: self.event(job_id, s, m)).deploy(
                Path(job["project"]), DeploymentPlan(**job["plan"]), job_id, environment)
            with self.lock:
                job.update(status="succeeded", result=result)
                self.save(job_id)
        except Exception as exc:
            message = str(exc)
            for value in sorted(set(environment.values()), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            self.event(job_id, "error", message)
            with self.lock:
                self.jobs[job_id]["status"] = "failed"
                self.save(job_id)
        finally:
            environment.clear()

    def retire_aws(self, job_id):
        try:
            with self.lock:
                job = json.loads(json.dumps(self.jobs[job_id]))
            result = job['result']
            attempt_id = result.get('owner_attempt') or result['service'].removeprefix('onedeploy-')
            adapter = AwsExpressAdapter(lambda stage, message: self.event(job_id, stage, message),
                                        AwsSettings(**job['aws']))
            adapter.retire(result, attempt_id)
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'delete_failed'
                self.jobs[job_id]['retire_error'] = str(exc)[:300]
                self.save(job_id)
            self.event(job_id, 'retire_failed', str(exc)[:300])
        else:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'deleted'
                self.jobs[job_id]['retired_at'] = datetime.now(timezone.utc).isoformat()
                self.jobs[job_id].pop('retire_error', None)
                self.save(job_id)
            self.event(job_id, 'retired', 'ECS 서비스와 해당 ECR 이미지 태그의 삭제를 확인했습니다.')

    def retire_local(self, job_id):
        try:
            with self.lock:
                job = json.loads(json.dumps(self.jobs[job_id]))
            LocalDockerAdapter(lambda stage, message: self.event(job_id, stage, message)).retire(
                job['result'], job_id)
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'delete_failed'
                self.jobs[job_id]['retire_error'] = str(exc)[:300]
                self.save(job_id)
            self.event(job_id, 'retire_failed', str(exc)[:300])
        else:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'deleted'
                self.jobs[job_id]['retired_at'] = datetime.now(timezone.utc).isoformat()
                self.jobs[job_id].pop('retire_error', None)
                self.save(job_id)
            self.event(job_id, 'retired', '로컬 Docker 컨테이너와 이미지 태그의 삭제를 확인했습니다.')

    def retire_cloud(self, job_id):
        try:
            with self.lock:
                job = json.loads(json.dumps(self.jobs[job_id]))
            result = job['result']
            attempt_id = result['service'].removeprefix('onedeploy-')
            CloudRunAdapter(lambda stage, message: self.event(job_id, stage, message),
                            CloudRunSettings(**job['cloud'])).retire(result, attempt_id)
        except Exception as exc:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'delete_failed'
                self.jobs[job_id]['retire_error'] = str(exc)[:300]
                self.save(job_id)
            self.event(job_id, 'retire_failed', str(exc)[:300])
        else:
            with self.lock:
                self.jobs[job_id]['deployment_state'] = 'deleted'
                self.jobs[job_id]['retired_at'] = datetime.now(timezone.utc).isoformat()
                self.jobs[job_id].pop('retire_error', None)
                self.save(job_id)
            self.event(job_id, 'retired', 'Cloud Run 서비스와 해당 Artifact Registry 이미지의 삭제를 확인했습니다.')

