# OneDeploy

**로컬 웹 앱을 올리고 배포 대상을 고르면, 코드 준비부터 실제 HTTP 확인까지 이어지는 배포 도구입니다.**
AI는 작업용 소스의 수정과 실행 설정을 돕습니다. 원본은 그대로 보존합니다.

> 개발 중인 개인 시제품입니다. Local Docker와 AWS ECS Express의 실제 배포를 확인했습니다.
> 실제 OpenAI 모델을 거친 전체 배포는 아직 검증하지 않았습니다. 테스트에는 고정 AI 응답을 사용했습니다.

## 빠른 시작

Python 3.11+, Docker Engine, OpenAI API 키가 필요합니다. Python 외부 패키지는 필요하지 않습니다.
`.env` 파일은 자동으로 읽지 않습니다.

```sh
export OPENAI_API_KEY='your-api-key'
python3 -m onedeploy.server
```

<http://127.0.0.1:8080>에서 앱 폴더 또는 ZIP을 올립니다. 앱 ID와 대상을 고른 뒤 **배포**를 누릅니다.
키가 없으면 호환성 미리보기는 사용할 수 있지만 AI 배포는 시작할 수 없습니다.
첫 실험에는 [설정이 미완성인 Node 앱](examples/unready-node)을 사용할 수 있습니다.

| 대상 | 준비 사항 | 검증 상태 |
| --- | --- | --- |
| Local Docker | Docker Engine | 빌드·실행·HTTP·종료 확인 |
| AWS ECS Express | AWS CLI, Docker, 계정 ID 고정 | 실제 배포·업데이트·PostgreSQL 경로 확인 |
| Google Cloud Run | `gcloud`, 프로젝트·리전 | 어댑터 테스트 통과. 실계정 배포 미검증 |

AWS 대상은 서버 실행 전에 `ONEDEPLOY_AWS_ACCOUNT_ID`를 설정해야 합니다.
실제 로그인 계정이 다르면 리소스 생성을 중단합니다. 현재 AWS 서비스는 공개 HTTPS만 지원합니다.
ECS·로드 밸런서·ECR·RDS에는 비용이 발생할 수 있습니다.

Cloud Run 대상은 `ONEDEPLOY_GCP_PROJECT`와 `ONEDEPLOY_GCP_REGION`이 필요합니다.
기본 접근 방식은 인증이 필요한 비공개 서비스입니다.

## 배포할 때 하는 일

1. 앱의 실행 방식과 데이터 요구를 검사합니다. 대상별 지원 여부와 근거 파일을 먼저 볼 수 있습니다.
2. AI가 작업용 복사본에서 코드와 실행 설정을 준비합니다. 서버는 AI의 제안과 대상 정책을 다시 검사합니다.
3. 이미지를 빌드해 배포합니다. 실제 HTTP 200을 확인해야 성공으로 기록합니다.
4. 이력에서 변경 내용, 실패 단계, 상태 검사와 배포 증명서를 볼 수 있습니다.

Node.js, 루트 진입점을 가진 Python, 기존 Dockerfile 웹 앱을 지원합니다.
Python ASGI·WSGI 앱은 필요한 서버 패키지를 `requirements.txt`에 명시해야 합니다.
업로드 한도는 20 MiB, 폴더 파일 5,000개입니다.

기본 배포 단위는 **단일 HTTP 컨테이너**입니다. 감지된 SQLite, 로컬 파일 저장, 별도 워커 등은
지원되는 영속 경로가 없으면 배포 전에 차단합니다. 정적 검사만으로 모든 요구를 찾을 수는 없습니다.

AWS에는 OneDeploy가 소유한 PostgreSQL RDS를 연결하는 경로가 있습니다.
기존 DB 사용을 명시하거나, 새 DB의 네트워크·용량·가격 계획을 확인한 뒤 생성할 수 있습니다.
SQL 마이그레이션과 데이터 응답도 실제 AWS에서 확인했습니다.
**SQLite 데이터 이전과 로컬 업로드 파일의 S3 이전은 아직 지원하지 않습니다.**

같은 앱 ID의 AWS 서비스는 URL을 유지하며 업데이트할 수 있습니다. 이전 릴리스로 되돌리는 경로도 있습니다.
Local Docker와 Cloud Run은 배포마다 별도 URL을 만듭니다. Local Docker는 현재 PC의 루프백에서만 접속됩니다.

## 운영과 검증

작업 기록은 `.onedeploy/`에 저장합니다. 서버를 다시 켜도 이력을 읽지만, 중단된 클라우드 요청은 자동 반복하지 않습니다.
AWS 결과가 불확실하면 실제 리소스를 재확인해야 합니다. 배포 증명서는 확인한 것과 미검증 항목을 구분합니다.

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_postgres_ui.mjs
```

GitHub Actions도 Python·JavaScript 테스트를 실행합니다. CI 결과는 실제 AWS 배포나 OpenAI 모델 검증을 대신하지 않습니다.
실환경 검증 조건은 [후속 검증 계획](docs/deferred-verification.md)에 적었습니다.

## 자세한 문서

- [현재 상태와 남은 작업](docs/status.md)
- [요구사항과 설계](docs/requirements.md) · [의사결정 기록](docs/decision-log.md)
- [AWS 데이터 경로](docs/aws-database-path.md) · [실계정 검증 기록](docs/aws-postgres-live-runbook.md)
- [Terraform 자원 기록](terraform/aws-live/README.md) · [개발 기록](docs/development-log.md)
