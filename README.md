# OneDeploy

**웹 앱을 올리면 AI가 배포에 필요한 코드와 실행 설정을 준비하고, 실제 배포와 HTTP 응답 확인까지 수행하는 도구입니다.**
원본 소스는 보존하고 작업용 복사본만 수정합니다. 현재는 단일 HTTP 컨테이너를 중심으로 지원하며,
AWS에서는 명시적으로 선택한 OneDeploy PostgreSQL RDS를 연결할 수 있습니다.

> 현재 구현은 개발 중입니다. AWS ECS Express와 Local Docker의 실제 배포는 확인했지만,
> **실제 OpenAI 모델의 코드 수정·대상 선택부터 배포까지 이어지는 끝단 검증은 아직 하지 않았습니다.** 통합 테스트의 AI 응답은 고정된 테스트 응답입니다. 정확한 진행 상태는
> [프로젝트 현황](docs/status.md)에 기록합니다.

## 시작하기

Python 3.11+, 실행 중인 Docker Engine, 그리고 Responses API 함수 호출을 지원하는 OpenAI 모델의
API 키가 필요합니다. Python 외부 패키지 의존성은 없습니다. `.env` 파일은 자동으로 읽지 않습니다.

```sh
export OPENAI_API_KEY='your-api-key'
export ONEDEPLOY_AI_MODEL='your-model-id'
python3 -m onedeploy.server
```

브라우저에서 <http://127.0.0.1:8080>을 엽니다. 키나 모델을 설정하지 않으면 AI 배포 버튼이 비활성화됩니다. UI에서 앱 폴더 또는 ZIP, 앱 ID, 배포 대상을
선택해 실행합니다. 첫 실험에는 [`examples/unready-node`](examples/unready-node) 폴더를 사용할 수 있습니다. 이 샘플은 시작 설정과 외부 접속 설정을
작업용 복사본에서 고치도록 설계했습니다.

| 배포 대상 | 추가 준비 | 현재 검증 |
| --- | --- | --- |
| Local Docker | Docker Engine | 빌드·실행·HTTP 응답과 종료 경로 검증 |
| AWS ECS Express | AWS CLI 로그인, Docker CLI, 고정 계정 ID | 실제 AWS 배포·업데이트·릴리스 전환·PostgreSQL 경로 검증 |
| Google Cloud Run | `gcloud` 로그인, 프로젝트·리전 | 어댑터 테스트 통과; 실제 GCP 배포는 미검증 |

AWS를 쓰려면 서버 실행 전에 `ONEDEPLOY_AWS_ACCOUNT_ID`에 대상 12자리 계정 ID를 설정합니다. 리전은 `ONEDEPLOY_AWS_REGION` 또는 AWS
CLI 기본 리전을 사용합니다. 실제 로그인 계정이 고정한 계정과 다르면 리소스 생성 전에 중단합니다. AWS ECS Express는 현재 **공개 HTTPS 서비스만** 지원하므로
UI에서 **인터넷에 공개하기**를 선택해야 합니다. ECR·ECS/Fargate·로드 밸런서·RDS 등 실제 AWS 자원에는 비용이 발생할 수 있습니다.

Cloud Run을 쓰려면 `ONEDEPLOY_GCP_PROJECT`와 `ONEDEPLOY_GCP_REGION`을 설정합니다.
필요하면 `ONEDEPLOY_GCP_REPOSITORY`와 `ONEDEPLOY_GCP_SERVICE_ACCOUNT`를 지정합니다.
Cloud Run은 기본적으로 인증이 필요한 비공개 서비스이며, 사용자가 공개를 선택할 수 있습니다.
실제 GCP 배포와 종료는 아직 계정에서 검증하지 않았습니다.

서버 포트는 `--port`, 상태 디렉터리는 `--state-dir`로 바꿀 수 있습니다. 성공한 활성 배포는 기본 5분마다 상태를 재검사하며,
`--monitor-interval 60`처럼 60~3600초로 조정하거나 `--monitor-interval 0`으로 끌 수 있습니다.

## 배포 흐름과 지원 범위

1. 앱 폴더 또는 ZIP을 올립니다. `package.json`이 있는 Node.js 앱이나 기존 `Dockerfile`이 있는 웹 앱을 받습니다. 폴더는 최대 5000개 파일,
   내용은 20 MiB까지 허용합니다.
2. AI가 소스 근거를 읽고 실행 설정·Dockerfile·필요한 코드 변경을 작업용 복사본에 적용합니다. 자동 대상 선택을 고르면 서버가 사용 가능한 대상, 공개 허용 범위, 지원
   인프라를 다시 검증합니다.
3. 실제 이미지를 빌드하고 배포한 뒤 HTTP 200을 확인해야 성공 처리합니다. 빌드·실행 실패는 로그를 AI에 전달해 최대 두 번 수정·재시도합니다. 필요한 환경변수 값은 진행 중
   별도로 요청합니다.
4. 작업 이력에서 현재 서비스 상태 확인, 배포 종료, AWS 업데이트 결과 재확인과 이전 릴리스 전환을 수행할 수 있습니다. AWS는 같은 앱 ID의 기존 서비스를 업데이트해 URL을
   유지합니다. Local Docker와 Cloud Run은 배포마다 별도 URL을 만듭니다.

기본 지원 범위는 영속 데이터·별도 워커가 없는 단일 HTTP 컨테이너입니다. SQLite·MySQL·MongoDB·로컬 파일 저장·백그라운드 워커 의존이 감지되면 리소스를 만들기 전에
차단합니다. AWS ECS Express에서는 사용자가 **기존 OneDeploy PostgreSQL RDS 사용**을 명시하고 소유권 검사를 통과한 앱만 PostgreSQL을 연결할 수
있습니다. 업로드 앱에는 `migrations/`의 SQL 파일과 `PGHOST`·`PGUSER`·`PGPASSWORD`·`PGDATABASE` 방식의 연결이 필요합니다. 정적 탐지는
모든 상태 저장·비동기 작업을 완전히 판별하지 못합니다.

## AWS PostgreSQL 사용

UI에서는 앱 ID의 전용 네트워크를 준비하고, 새 RDS의 계정·서브넷·용량 가격을 **읽기 전용 계획**으로 확인한 뒤 별도 생성 버튼을 누를 수 있습니다. 앱 전용 네트워크 스택
대신 서버에 `ONEDEPLOY_AWS_SERVICE_SECURITY_GROUP`을 지정할 수도 있습니다. 생성한 DB는 비공개·암호화·삭제 보호 상태로 유지되며, 앱 배포 실패나 ECS
서비스 종료만으로 삭제되지 않습니다. 기존 DB를 연결할 때는 앱 ID와 **기존 OneDeploy PostgreSQL RDS 사용** 선택만 필요합니다. 서버가 배포 요청 시 소유 DB의 VPC·서브넷을 재조회합니다. UI의 **이 앱의 기존 RDS 조회**는 배포 전 상태 확인용입니다.

이미 생성된 앱 소유 PostgreSQL을 명시적으로 선택했다면 배포 대상을 **AI 자동 선택**으로 두어도 됩니다. 이 경우 PostgreSQL을 지원하는 대상이 AWS ECS Express 하나이므로 서버 정책이 AWS를 선택하고, 계획에 그 이유를 기록합니다. 앱 코드 준비에는 계속 AI 배포 에이전트를 사용합니다. 새 DB 생성은 별도의 명시적 계획·생성 단계가 필요합니다.

같은 화면에서 자동 백업·삭제 보호를 조회하고, 명시적 계획을 거쳐 수동 스냅샷을 생성할 수 있습니다. **PostgreSQL RDS 폐기**는 현재 ECS 서비스·작업의 해당 비밀
사용을 검사하고 최종 암호화 수동 스냅샷이 `available`로 확인된 뒤 DB와 RDS 스택을 삭제합니다. 기존·최종 수동 스냅샷과 앱 네트워크는 남으며 저장 비용이 계속 발생할 수
있습니다. 접수된 작업은 로컬에 먼저 기록하고, 서버가 중단되면 자동 재실행하지 않습니다.

RDS 생성이 실패해 앱 소유 스택이 `ROLLBACK_COMPLETE`가 되면 UI의 [실패 생성 정리 절차](docs/aws-postgres-failed-create-recovery.md)로 잔여 DB·스냅샷 부재를 확인하고 실패 스택만 정리할 수 있습니다. 정리 완료 후 같은 앱 ID로 새 생성 계획을 요청합니다.

2026-10-03까지 임시 앱에서 네트워크·RDS 생성, 앱 업로드·SQL 마이그레이션·HTTP 데이터 쓰기/읽기, 실제 Chrome의 유효 DB ID 폐기 실행과 최종 스냅샷 선행 DB 삭제를
확인했습니다. 시험용 자원은 정리했고 보존 중인 `demo-app` DB와 스냅샷은 유지했습니다. 스냅샷 복원·읽기 전용 SQL 원장 및 데이터 표식 검사도 별도 임시 DB로 통과했습니다. 세부 절차와 비용·복구 경계는
[AWS 데이터 경로](docs/aws-database-path.md)와 [실계정 검증 기록](docs/aws-postgres-live-runbook.md)에 있습니다.

2026-10-03 데모 AWS 실행 자원을 내일 재개할 수 있게 일시 중지했다. RDS는 `stopped`, ECS 태스크는 0개이며 서비스·로드 밸런서·스토리지 등은 남아 있다. [Terraform 기록과 재개 절차](terraform/aws-live/README.md), [중지 상태 검증](docs/aws-pause-2026-10-03.md)을 참고한다.

새 RDS를 만든 앱은 생성 작업이 완료되고 기록된 DB ID와 실제 소유 DB가 일치해야
ZIP 배포를 시작할 수 있습니다. 생성 결과가 불확실하면 먼저 상태 재확인 또는
실패 스택 정리를 진행합니다.
AWS 대상에서는 앱 폴더·ZIP을 선택하고 신규 RDS의 VPC·서브넷·용량 가격 계획을
확인한 뒤 **“이 계획으로 RDS 생성 후 앱 배포”**를 누르면 한 작업에서 DB 생성
확인 → SQL 마이그레이션 → ECS 배포로 이어집니다. 생성 뒤 앱 배포가 실패하거나
배포 시작 전 취소해도 DB는
보존됩니다. 서버 재시작이나 생성 결과 불확실 시 자동으로 다시 생성·배포하지
않습니다. 이 연결 경로는 현재 로컬 모의 AWS 테스트만 통과했고, 중지된 실계정
자원으로는 아직 실행하지 않았습니다.

## 작업 기록과 안전 경계

OneDeploy는 업로드 원본과 수정용 복사본, 실제 빌드 시도, 상태·변경 diff·로그를 `.onedeploy/` 아래에 저장합니다. 이 디렉터리는 Git에서 제외합니다. 서버 재시작
후 이력을 복원하되 실행 중이던 작업은 자동으로 재실행하지 않습니다. 상태 확인은 저장된 소유 리소스와 HTTP 응답을 다시 검사합니다.
배포 작업은 첫 빌드·클라우드 시도가 시작되기 전까지만 취소할 수 있습니다. 진행 중인 AI 응답은 끝난 뒤 취소를 확인합니다. 첫 시도 이후에는 리소스 생성 결과가 불확실할 수 있어 취소 요청을 거부하며 작업 결과와 복구 경로를 확인해야 합니다.

환경변수 입력값은 AI에 전달하거나 `job.json`·Dockerfile에 저장하지 않습니다. 클라우드 CLI에는 권한 0600 임시 파일로 전달합니다. 다만 Docker 엔진과
클라우드 서비스는 런타임 값을 보관하며, 변형·인코딩된 비밀값까지 로그에서 완전히 마스킹한다고 보장하지 않습니다. 코드와 로그 일부는 AI API에 전송되므로 직접 만든 신뢰할 수 있는
앱을 사용하세요. 업로드에서 `.env`, `.git`, `node_modules` 등은 제외합니다. 이 구현은 비신뢰 코드를 위한 완전한 격리 플랫폼이나 다중 사용자 서비스가
아닙니다.

## 검증

GitHub Actions는 `main` 푸시와 PR에서 Python 3.11·3.14 단위 테스트 및 Node 22의
UI·PostgreSQL JavaScript 테스트를 실행합니다. 이 CI에는 AWS 자격 증명이 없으며
실제 Docker·브라우저·클라우드 배포 드릴은 포함하지 않습니다.

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node tests/test_postgres_ui.mjs
node tests/test_postgres_migrator.js
node tests/test_postgres_restore_verifier.mjs
python3 -m tests.smoke_local_auto_postgres_browser
python3 -m tests.smoke_local_postgres_one_action_browser
PYTHONPATH=. python3 tests/smoke_agent.py
```

첫 로컬 Chrome 드릴은 기존 RDS 자동 연결 계획을 검사합니다. 두 번째 드릴은 새 RDS 가격 계획부터 ZIP 업로드, 동일 작업의 DB 생성·배포 완료 표시와 생성 실패 후 복구 버튼 노출까지 모의 AWS·배포 응답으로 검사합니다. 둘 다 실제 AWS 호출이나 배포를 실행하지 않습니다. 마지막 명령은 Docker를 사용하고 AI의 도구 응답만 테스트용으로 고정합니다. 실제 OpenAI 호출을 검증할 때는 키·모델을 설정하고
`PYTHONPATH=. python3 tests/smoke_agent.py --live`를 사용합니다. AWS 실계정 smoke는 기본이 읽기 전용 사전 점검이며, 과금 가능한 리소스를
생성하는 `--apply` 절차와 정리 방법은 [실계정 검증 기록](docs/aws-postgres-live-runbook.md)에 적었습니다. 현재 구현·실계정 증거·남은 작업의 구분은
[프로젝트 현황](docs/status.md)을 따릅니다.

## 문서

- [개발 기록 안내](docs/development-log.md): 커밋 이력, 검증 기록, 의사결정 문서의 시작점
- [요구사항](docs/requirements.md) · [설계](docs/design.md): 해커톤 주제 해석과 시스템 구조
- [프로젝트 현황](docs/status.md): 검증된 기능과 우선순위별 남은 작업
- [의사결정 기록](docs/decision-log.md): 구현·복구·안전 경계의 판단 근거
- [AWS 데이터 경로](docs/aws-database-path.md) · [실계정 검증 기록](docs/aws-postgres-live-runbook.md): 상세 명령과 실험 결과
- [데모 흐름](docs/demo-script.md) · [발표 초안](docs/talk-script-ko.md)
