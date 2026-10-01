# OneDeploy

**앱을 올리고 배포를 요청하면, AI가 필요한 설정·코드를 준비하고 실제 실행까지 진행하는 배포 도구.**

현재 구현·실계정 검증·남은 작업은 [프로젝트 현황](docs/status.md)에 정리했습니다.

현재 구현은 Node.js 앱 또는 기존 Dockerfile을 가진 웹 앱 → Local Docker, Google Cloud Run 또는 AWS ECS Express Mode 경로입니다. UI에서 앱 폴더 또는 ZIP·앱 ID·자동/수동 배포 대상을 선택하고
**배포하기**를 누르면 작업용 소스의 읽기·수정, Dockerfile 준비, 빌드·실행, HTTP 검증을 진행합니다.
자동 선택에서는 AI가 소스 근거와 이유를 제시하고, 서버가 현재 사용 가능한 대상·공개 범위·지원 작업 유형을 검증합니다.
일반 배포에서 실행 가능한 인프라는 **영속 데이터와 별도 워커가 없는 단일 HTTP 컨테이너**입니다. AWS ECS Express Mode에서는 기존 OneDeploy PostgreSQL RDS를 명시해 연결하는 제한된 경로도 있습니다. 그 외 SQLite·MySQL·MongoDB·로컬 파일 저장·백그라운드 워커 의존이 감지되면 데이터 손실이나 작업 누락을 막기 위해 배포를 시작하지 않습니다.
기존 RDS를 연결하려면 **AWS ECS Express Mode**와 **인터넷에 공개하기**를 선택하고 **기존 OneDeploy PostgreSQL RDS 사용**을 체크하세요. **이 앱의 기존 RDS 조회**를 눌러 서버가 소유권을 확인한 VPC·서브넷 ID를 채우거나, 해당 ID를 직접 입력합니다. 서버에는 `ONEDEPLOY_AWS_ACCOUNT_ID`를 고정해야 합니다. 앱별 네트워크 스택을 쓰지 않는 기존 경로에는 `ONEDEPLOY_AWS_SERVICE_SECURITY_GROUP`도 지정합니다. 업로드 앱은 PostgreSQL 단일 엔진과 `migrations/` SQL 묶음을 사용해야 합니다. 서버가 DB 소유권을 확인한 뒤 배포하며, 이 흐름은 DB를 새로 만들지 않습니다. ECS와 기존 RDS 비용이 발생하고 앱 종료 후에도 DB는 남습니다.
**백업·보호 상태 확인**은 같은 앱 RDS의 소유권을 다시 검증하고 자동 백업 보존 기간·최근 복원 가능 시점·삭제 보호·수동 스냅샷 수를 읽기 전용으로 보여줍니다. 이는 복원 시험이 아니며 수동 스냅샷을 생성하지 않습니다. 실제 `demo-app` DB에서는 보존 7일·삭제 보호 켜짐을 확인했고, 2026-10-02 현재 수동 스냅샷은 1개입니다.
수동 스냅샷은 `python3 -m onedeploy.postgres_snapshot --application <APP_ID> --snapshot-id onedeploy-<APP_ID>-<NAME> --account <AWS_ACCOUNT_ID> --region <AWS_REGION> [--service-security-group <GROUP_ID>]`로 먼저 읽기 전용 계획을 확인합니다. 생성은 같은 명령에 `--apply`를 붙여야 하며, 이후 `--inspect`로 소유 태그·암호화·상태를 재확인합니다. 스냅샷은 삭제 전까지 보관되어 백업 저장 비용이 발생할 수 있습니다.
UI의 **기존 RDS 수동 스냅샷**에서도 스냅샷 이름을 입력하고 읽기 전용 계획을 확인한 뒤 별도 생성 버튼을 누를 수 있습니다. 서버는 생성 요청을 먼저 기록하고 비동기로 실행합니다. 서버 재시작이나 AWS 응답이 불확실하면 자동 재시도하지 않고 **AWS 스냅샷 상태 재확인**으로 같은 스냅샷을 검사합니다. 2026-10-02 실제 Chrome에서 `onedeploy-demo-app-backup-20261002` 생성·완료 확인을 통과했습니다. 원본 DB는 `available`이고 암호화된 스냅샷은 보존 중입니다. 스냅샷 복원 시험은 아직 하지 않았습니다.
복원 드릴 준비는 `python3 -m onedeploy.postgres_restore --application <APP_ID> --snapshot-id <SNAPSHOT_ID> --target-id onedeploy-restore-<APP_ID>-<NAME> --account <AWS_ACCOUNT_ID> --region <AWS_REGION> [--service-security-group <GROUP_ID>]`로 읽기 전용 점검합니다. 스냅샷 소유권·원본 구성·새 DB ID 중복·기본 용량 가격을 확인합니다. `onedeploy.postgres_restore_network` CLI는 대상별 보안 그룹을 기본 읽기 전용 점검 후 `--apply`로 생성하고, 규칙·태그를 검증하거나 미사용 상태에서 `--delete <GROUP_ID>`로 정리합니다. 임시 그룹의 실제 생성·정리까지 통과했습니다. DB 복원 인스턴스의 생성·정리 경로는 아직 연결되지 않아 복원 계획 명령에는 적용 옵션이 없습니다.
이 경로의 배포 작업 기록에는 기존 RDS 연결과 일회성 SQL 마이그레이션 작업이 구분돼 표시됩니다. 실제 AI 모델 호출을 포함한 끝단 검증은 아직 수행하지 않았습니다.
앱 전용 ECS 보안 그룹은 `python3 -m onedeploy.aws_network --application <APP_ID> --account <AWS_ACCOUNT_ID> --region <AWS_REGION> --vpc-id <DEFAULT_VPC_ID>`로 읽기 전용 사전 점검하고, 실제 생성할 때만 `--apply`를 추가합니다. 서버에서 `ONEDEPLOY_AWS_SERVICE_SECURITY_GROUP`을 비워 두면 기존 RDS 조회·신규 RDS 계획·DB 앱 업로드 시 앱 ID와 VPC가 일치하는 `onedeploy-network-<APP_ID>` 스택을 읽기 전용으로 검증해 그룹을 선택합니다. 스택이 없거나 검증에 실패하면 작업을 거부합니다. 고정 그룹 설정은 기존 `demo-app` 경로에 그대로 사용할 수 있습니다. 임시 앱 네트워크 스택의 생성·앱별 그룹 선택은 실제 AWS에서 검증했으며 신규 RDS 배포는 아직 검증하지 않았습니다.
UI의 **앱 전용 AWS 네트워크 준비**에서도 앱 ID·기본 VPC의 읽기 전용 계획을 확인한 다음 별도 버튼으로 네트워크 생성을 요청할 수 있습니다. 서버는 계획 ID와 생성 작업·스택 ARN을 기록하고 비동기로 실행합니다. 재시작으로 결과가 불확실하면 자동 재시도하지 않고 **AWS 네트워크 결과 재확인**으로 검증합니다. 이 UI 생성 경로는 실제 Chrome과 AWS에서 임시 스택 생성·검증·정리를 통과했습니다. 신규 RDS 배포는 아직 검증하지 않았습니다.
**기본 VPC·서브넷 불러오기**는 고정한 AWS 계정의 기본 VPC와 서로 다른 가용 영역의 사용 가능한 기본 서브넷을 읽기 전용으로 조회해 네트워크·새 RDS 계획 입력에 채웁니다. 조회 후에도 각 생성 계획의 사전 검증이 필요합니다. 서울 리전의 실제 계정에서 이 조회를 확인했으며 새 리소스는 만들지 않았습니다.
실제 Chrome에서 이 자동 입력과 기존 RDS 조회를 배포 없이 확인하려면 [AWS 실계정 검증 절차](docs/aws-postgres-live-runbook.md)의 `--browser-read-only` 모드를 사용합니다.
새 DB가 필요한 경우 AWS 대상의 **새 PostgreSQL RDS 생성 계획 미리보기**에서 기본 VPC·서로 다른 가용 영역의 서브넷을 입력해 계정·네트워크·주문 가능 구성·기본 용량 가격을 읽기 전용으로 확인할 수 있습니다. 계획 조회는 DB를 만들지 않으며, 같은 앱 ID의 CloudFormation 스택 기록이 있으면 신규 계획을 거부합니다. 이때는 기존 RDS 조회를 사용하세요. 표시된 구성과 비용을 확인한 뒤 **이 계획으로 RDS 생성**을 누르면 생성 작업을 기록하고 AWS CloudFormation을 비동기로 실행합니다. 계획 전에 고정 서비스 보안 그룹을 설정하거나 앱 전용 네트워크 스택을 생성해야 합니다. 서버 재시작 등으로 결과가 불확실하면 **AWS 생성 결과 재확인**으로 소유 스택을 읽기 전용으로 검사합니다. 기존 RDS의 재사용과 달리 신규 생성 UI의 실계정 생성 실행은 아직 검증하지 않았습니다.
빌드 또는 실행이 실패하면 결과를 AI에 전달하고 최대 두 번 수정·재시도합니다.
분석 결과를 검토·승인하는 단계는 없습니다. 필요한 환경변수 값만 진행 중에 요청합니다.

## 실행

Python 3.11+와 실행 중인 Docker Engine이 필요합니다. Python 외부 패키지 의존성은 없습니다.
서버 환경에 `OPENAI_API_KEY`와 `ONEDEPLOY_AI_MODEL`을 설정하세요. 모델은 계정에서 사용할 수 있고
Responses API 함수 호출을 지원하는 모델 ID를 직접 지정합니다. `.env` 자동 로딩은 하지 않습니다.

```sh
python3 -m onedeploy.server
```

http://127.0.0.1:8080 에서 사용합니다. API 키가 없으면 AI 배포 버튼은 비활성화됩니다.
AI를 모방하는 규칙 기반 동작으로 몰래 전환하지 않습니다.
서버가 실행되는 동안 성공한 활성 배포는 기본 5분마다 소유 리소스와 HTTP 200을 자동 재검사합니다.
`--monitor-interval 60`처럼 60~3600초로 조정하거나 `--monitor-interval 0`으로 끌 수 있습니다.

배포 준비가 안 된 샘플은 UI에서 `examples/unready-node` 폴더를 바로 선택할 수 있습니다. ZIP으로도 올리려면 다음 명령을 사용합니다.

```sh
python3 -m zipfile -c /tmp/onedeploy-unready.zip examples/unready-node
```

이 앱은 시작 스크립트와 Dockerfile이 없고 `127.0.0.1:4321`에 바인딩합니다.
AI가 작업용 복사본에서 시작 스크립트와 외부 접속 설정을 수정한 뒤 배포하도록 설계했습니다.
HTTP 응답에 `Original application is running`이 표시되면 원래 앱의 실행을 확인할 수 있습니다.
**실제 AI API 호출은 현재 키 미설정으로 미검증**입니다. 아래 테스트 결과와 구분하세요.
인프라 자동 선택을 포함한 로컬 통합 검증은 `PYTHONPATH=.:tests python3 tests/smoke_agent.py --folder --auto`로 실행합니다.
기본 모드의 자동 선택과 코드 수정 판단은 고정 테스트 응답이며, 실제 모델을 함께 쓰려면 `--live`를 추가하고 키·모델을 설정해야 합니다.

## Google Cloud Run 연결

Cloud Run 대상은 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)가 설치되고 로그인되어 있어야 합니다.
서버를 실행할 환경에 다음 값을 설정하세요.

```sh
export ONEDEPLOY_GCP_PROJECT=your-project-id
export ONEDEPLOY_GCP_REGION=asia-northeast3
# 선택 사항: 기본 저장소 이름은 onedeploy
export ONEDEPLOY_GCP_REPOSITORY=onedeploy
```

필요한 API, Docker 형식의 Artifact Registry 저장소와 전용 실행 서비스 계정은 배포 중 준비합니다.
이미 존재하면 사용합니다. 다른 실행 계정을 쓰려면 `ONEDEPLOY_GCP_SERVICE_ACCOUNT`에 같은 프로젝트의
계정 이메일을 설정하세요. 현재 로그인한 계정에는 API 활성화, 저장소 생성·업로드, 서비스 계정 생성·사용,
Cloud Run 배포·로그 조회 권한이 필요합니다. 권한이 부족하면 작업 기록에 실패 원인을 남깁니다.

Cloud Run을 선택하면 이미지를 `linux/amd64`로 빌드해 저장소에 업로드하고 서비스로 배포합니다.
기본은 **인증이 필요한 비공개 서비스**입니다. 화면에서 ‘인터넷에 공개하기’를 직접 선택하면 공개 서비스로
배포합니다. 서비스의 CPU·메모리·최대 인스턴스 수에 상한을 두지만 실제 비용은 프로젝트에 청구될 수 있습니다.
Cloud Run URL의 HTTP 응답을 확인한 뒤 완료합니다. 비공개 서비스의 URL은 일반 브라우저에서 바로 열리지 않으며,
CLI 계정에 Cloud Run Invoker 권한이 있어야 배포 검증을 완료할 수 있습니다.

현재 개발 환경에는 `gcloud` CLI와 프로젝트 설정이 없어 **실제 Cloud Run 배포는 아직 실행해 검증하지 못했습니다.**
클라우드 어댑터의 명령·인증 전달·리소스 소유 확인은 테스트 값으로 검증했습니다.
성공한 배포는 **이 Cloud Run 배포 종료** 버튼 또는 `POST /api/jobs/<id>/retire`로 종료할 수 있습니다.
서비스의 관리 라벨·URL·이미지를 확인한 뒤 서비스 삭제 완료를 기다리고 해당 Artifact Registry 이미지를 삭제합니다.
이미지에 다른 태그가 붙었다면 삭제 명령이 실패하도록 `--delete-tags`를 사용하지 않습니다.
공유 저장소와 실행 서비스 계정은 유지합니다. 종료 경로 역시 현재는 테스트 값으로만 검증했습니다.

## AWS ECS Express Mode 연결

AWS CLI와 Docker CLI가 필요합니다. AWS CLI에 로그인된 계정과 리전이 있어야 합니다.
리전은 `ONEDEPLOY_AWS_REGION`으로 지정하거나 AWS CLI의 기본 리전을 사용합니다.
서버에서 AWS 배포를 사용하려면 `ONEDEPLOY_AWS_ACCOUNT_ID`에 배포할 12자리 계정 ID도 설정하세요.
실제 로그인 계정이 이 값과 다르면 CloudFormation 등 유료 리소스를 만들기 전에 중단합니다.
계정 ID를 설정하지 않으면 AWS 대상은 선택할 수 없습니다.
기본 VPC의 기존 보안 그룹을 ECS 태스크에 추가하려면 `ONEDEPLOY_AWS_SERVICE_SECURITY_GROUP=sg-...`를
설정할 수 있습니다. 배포 전 해당 그룹이 기본 VPC에 있고 **인바운드가 비어 있거나 같은 VPC·계정의 보안 그룹에서 앱의 단일 포트로 들어오는 규칙 하나만 있는지** 읽기 전용으로
확인합니다. 생성 요청과 실제 ECS 구성에 이 그룹이 적용됐는지 확인하며, 같은 앱의 업데이트에서는
처음 사용한 그룹을 바꿀 수 없습니다. 이 설정은 추가 네트워크 경로의 기반일 뿐 RDS나 DB 접속을
프로비저닝하지 않으며, DB 의존 앱 차단도 그대로 유지됩니다.
별도 PostgreSQL 리소스 생성 명령과 남은 연결 작업은 [AWS 데이터 배포 경로](docs/aws-database-path.md)에 기록했습니다.

AWS 대상을 선택하고 **인터넷에 공개하기**를 명시적으로 선택하면, 배포 중
[`onedeploy-core` CloudFormation 템플릿](onedeploy/infra/aws-ecs-express.yaml)을 적용합니다.
이 스택은 `onedeploy-managed` ECR 저장소, ECS 태스크 실행 역할, Express Mode 인프라 역할을 만듭니다.
그 뒤 이미지를 ECR에 업로드하고 ECS Express Mode 서비스를 생성합니다. 같은 앱 ID의 다음 AWS 배포는
소유권을 확인한 뒤 기존 서비스의 새 리비전으로 업데이트합니다. AWS가 Fargate 태스크,
Application Load Balancer와 네트워크·확장 리소스를 구성합니다. 서비스 최소/최대 태스크 수는 1로
제한했습니다. 재배포는 새 리비전이 `SUCCESSFUL`이고 새 이미지가 활성화돼야 하며,
최초 배포와 재배포 모두 ECS 배포가 `SUCCESSFUL`이고 해당 이미지 하나만 활성화된 뒤
HTTPS URL이 HTTP 200을 반환해야 성공으로 표시합니다.

이 경로는 현재 **공개 HTTPS 서비스만** 지원합니다. CloudFormation 기반 리소스는 재사용하며,
실패한 배포에서는 이번 시도의 관리 태그가 확인된 ECS 서비스만 삭제를 시도합니다. 실패해도
공유 스택은 남습니다. 성공한 서비스와 ECR 이미지도 명시적으로 정리하기 전까지 남아 비용이
발생할 수 있습니다. 런타임 환경변수 값은 권한 0600 임시 JSON 파일로 AWS CLI에 전달하고,
ECS 서비스 구성에 평문 환경변수로 저장됩니다. 실무 비밀값에는 AWS Secrets Manager 연동이 필요합니다.
최초 배포가 서비스 생성 후 실패하면 관리 태그와 서비스 ARN을 확인해 서비스를 종료하고,
`INACTIVE`가 확인된 뒤 해당 ECR 이미지 태그를 삭제합니다. 소유권·종료 상태가 불확실하면 이미지를
보존하고 작업 기록에 수동 확인이 필요함을 남깁니다.

성공한 AWS 배포는 작업 화면의 **이 AWS 배포 종료** 버튼으로 종료할 수 있습니다.
`POST /api/jobs/<id>/retire`는 저장된 AWS 계정·서비스 ARN·이미지와 실제 서비스의 관리 태그를
확인한 뒤 서비스를 삭제합니다. ECS가 `INACTIVE`가 되면 해당 서비스의 릴리스 이미지 태그들을 삭제하고
작업 이력에 종료 상태를 남깁니다. 공유 CloudFormation 스택과 다른 배포는 유지합니다.
종료 도중 서버가 재시작되거나 AWS 호출이 실패하면 종료 실패 상태로 기록하고 같은 작업에서
다시 시도할 수 있습니다.

성공한 Local Docker 배포도 작업 화면의 **이 로컬 배포 종료** 버튼 또는 같은 종료 API로
관리할 수 있습니다. 저장된 작업 ID·컨테이너 이름·이미지 태그와 실제 Docker 관리 라벨을
대조한 뒤 해당 컨테이너와 이미지 태그만 삭제하고 작업 이력에 종료 결과를 남깁니다.
도중에 중단되거나 Docker 확인에 실패하면 같은 작업에서 다시 시도할 수 있습니다.

2026-09-28 서울 리전의 실제 AWS 계정에서 CloudFormation 스택, ECR 이미지, ECS Express 서비스를
생성하고 공개 HTTPS 주소의 HTTP 200과 샘플 앱 JSON 응답을 확인했습니다. 이 실계정 smoke는
**AWS 배포 어댑터**를 검증했습니다. 이어서 업로드 API부터 테스트용 고정 도구 호출,
실제 소스 수정·AWS 배포·상태 재검사까지의 경로도 통과했습니다. 실제 AI API 판단은 아직 미검증입니다.
AWS가 반환한 공개 주소는 서비스 이름과 다른 `on-…ecs.ap-northeast-2.on.aws` 형식이었고,
서비스가 `ACTIVE`가 된 뒤에도 로드 밸런서·인증서·DNS 준비에 시간이 더 걸렸습니다.
로컬 DNS가 새 주소를 늦게 반영할 때는 공개 DNS의 IP로 연결하되 TLS 호스트명 검증을 유지합니다.
실제 계정에서 v1→v2 업데이트가 같은 서비스 URL을 유지하고 v2 응답을 반환하는 경로도 확인했습니다.
AWS 업데이트 명령을 호출하기 직전에 작업 상태를 저장합니다. 호출 뒤 실패하거나 서버가 중단되면
OneDeploy는 자동 재시도를 중단하고 이전 릴리스를
**상태 확인 필요**로 표시합니다. 새 배포를 시작하기 전에 실패 작업의 **AWS 업데이트 결과 재확인**을
누르세요. `POST /api/jobs/<실패 또는 중단된 작업 ID>/reconcile`은 ECS 배포의 종료 상태와 실제 이미지·HTTP 200을
읽기 전용으로 확인해, 새 릴리스가 정상이라면 성공으로 복구하고 이전 릴리스로 롤백됐다면 기존 릴리스를
다시 활성으로 표시합니다. 두 결과 중 어느 쪽도 확인되지 않으면 배포 차단을 유지합니다.
AWS에 새 배포 이력이 전혀 나타나지 않는 시간 초과 사례는 실패 또는 서버 재시작 후 10분을 기다린 뒤,
진행 중인 배포가 없고 기존 이미지만 활성 상태이며 HTTP 200인 경우에만 이전 릴리스를 복구합니다.
외부에서 기존 이미지로 다시 배포한 경우에도 완료 상태와 실제 이미지를 확인합니다.
이 경로는 단위 테스트로 검증했으며 실제 AWS에서 실패·롤백을 의도적으로 유발해 검증하지는 않았습니다.
이전 릴리스로 복구된 작업에는 **실패한 ECR 이미지 정리** 버튼이 나타납니다.
`POST /api/jobs/<실패 작업 ID>/cleanup-image`는 AWS 계정·서비스 관리 태그·완료된 배포·기존 이미지의
단독 실행·HTTP 200을 다시 확인한 뒤 해당 실패 시도의 ECR 태그 하나만 삭제합니다. 정리 중에는
같은 앱의 새 배포와 서비스 종료를 막고, 실패하거나 서버가 재시작되면 재시도할 수 있습니다.
해당 태그가 이미지의 마지막 태그라면 ECR 이미지 자체도 삭제됩니다.
이미지 정리는 자동으로 실행하지 않으며, 실제 AWS 실패 이미지 정리 경로는 아직 단위 테스트로만 검증했습니다.
실패하거나 중단된 AWS 업데이트가 **진행 중**이면 **진행 중 AWS 배포 롤백 요청** 버튼으로
`POST /api/jobs/<작업 ID>/rollback`을 호출할 수 있습니다. OneDeploy는 서비스 관리 태그와
배포의 이전·새 리비전 태스크 정의 이미지를 확인한 뒤 ECS에 이전 리비전 롤백을 요청합니다.
이는 [AWS의 진행 중 서비스 배포 롤백](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/stop-service-deployment.html)을 사용합니다.
요청은 완료를 뜻하지 않습니다. AWS 배포 상태가 끝난 다음 **AWS 업데이트 결과 재확인**으로
실행 중인 릴리스를 확정하세요.
이 롤백 요청 경로는 단위 테스트와 기존 서비스의 태스크 정의 응답 형식만 검증했으며,
실제 진행 중인 AWS 배포를 중단해 보지는 않았습니다.

완료된 AWS 릴리스는 화면의 **전환할 AWS 릴리스**에서 같은 서비스의 성공 이력을 선택해 되돌릴 수 있습니다.
`POST /api/jobs/<현재 작업 ID>/rollback-release`에 `{"target_job_id":"<대상 작업 ID>"}`를 보내면
저장된 대상 태스크 정의와 실제 ECS 이미지·관리 태그를
대조하고 [AWS의 태스크 정의 지정 업데이트](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-update-full.html)를
요청합니다. 새 배포의 성공, 이전 이미지 활성화, 같은 HTTPS URL의 HTTP 200을 확인한 뒤 이력을 갱신합니다.
요청 후 서버가 중단되거나 결과가 불확실하면 **릴리스 롤백 결과 재확인**으로 실제 ECS 상태를 조회합니다.
재확인은 활성 이미지와 태스크 정의가 저장된 릴리스와 모두 일치하고 단일 구성이 안정화됐을 때만 이력을 복구합니다.
대상 릴리스의 태스크 정의와 이미지가 남아 있어야 하며, 전환 후 새 배포는 활성화된 릴리스를 기준으로 진행합니다.
실계정 임시 서비스에서 v1→v2 업데이트 후 같은 URL의 v1 응답으로 복구되는 경로를 확인했습니다.
검증용 ECS 서비스는 `INACTIVE`가 됐고 두 테스트 이미지 태그를 삭제했습니다. 기존 데모 서비스는 유지했습니다.
2026-09-29에는 별도 임시 서비스에서 v1→v2→v3 후 v1으로 복구해 같은 HTTPS URL의 v1 응답을 확인했습니다.
배포 `SUCCESSFUL` 직후 잠시 두 구성이 함께 보이는 AWS 응답을 발견해, 업데이트 완료는 단일 활성 구성과
`currentDeployment` 해소까지 기다리도록 했습니다. 이 임시 서비스와 이미지 태그 세 개도 정리했습니다.
실계정 smoke 스크립트는 실행 전에 계정 ID를 다시 확인하며, `--apply` 없이는 리소스를 생성하지 않습니다.

```sh
PYTHONPATH=. python3 tests/smoke_aws.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2
# 실제 리소스 생성 시에만 위 명령에 --apply 추가
```

이 smoke는 샘플 Node.js 앱을 직접 AWS에 배포해 어댑터를 검증합니다. AI 환경변수가 없는 환경에서도
실행할 수 있으나, OneDeploy UI의 실제 AI 판단까지 검증하는 것은 아닙니다. 성공한 ECS 서비스,
ECR 이미지와 기반 스택은 자동 삭제하지 않고 반환된 ARN·이미지 태그를 남깁니다.

업로드 API와 작업용 소스 수정까지 포함한 AWS 통합 smoke는 다음과 같습니다. AI 도구 호출은
테스트용 고정 응답이며, 생성한 ECS 서비스와 이미지 태그는 검증 후 삭제를 요청합니다.

```sh
PYTHONPATH=.:tests python3 tests/smoke_aws_agent.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --apply
PYTHONPATH=.:tests python3 tests/smoke_aws_agent.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --python --apply
PYTHONPATH=. python3 tests/smoke_aws_update.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --apply
PYTHONPATH=. python3 tests/smoke_aws_update.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --apply --rollback
PYTHONPATH=. python3 tests/smoke_aws_update.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --apply --rollback-oldest
```

실제 OpenAI 모델이 소스를 수정하고 AWS에 배포하는 전체 경로는 별도 smoke로 검증합니다.
`OPENAI_API_KEY`와 `ONEDEPLOY_AI_MODEL`을 설정하고, 먼저 `--apply` 없이 계정·도구 사전 점검을 실행하세요.
`--apply`를 붙이면 실제 API 호출과 비용이 발생할 수 있는 ECS/ECR 리소스 생성이 시작됩니다.
이 smoke는 고정 AI 응답을 사용하지 않고, 소스 패치·공개 HTTPS 200·상태 API·서비스 종료를 확인합니다.
종료가 확인되지 않으면 작업 기록 경로를 남기므로 AWS 리소스를 확인해야 합니다. 공유 CloudFormation 스택은 유지합니다.

```sh
PYTHONPATH=.:tests python3 tests/smoke_aws_live_ai.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2
PYTHONPATH=.:tests python3 tests/smoke_aws_live_ai.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --apply
```

현재 개발 환경에는 OpenAI API 키가 없어 이 live smoke의 실계정 결과는 아직 없습니다.

`--python` smoke는 `package.json` 없이 Dockerfile을 가진 Python 앱의 업로드·작업용 소스 수정·AWS 배포·공개 HTTP 200·상태 API를 검증합니다.
2026-09-29 서울 리전 실계정에서 이 경로가 통과했고, 임시 서비스의 `INACTIVE`와 ECR 태그 부재를 확인했습니다.

## 현재 범위

- 입력: `package.json`이 있는 단일 Node.js 앱 또는 `Dockerfile`이 있는 단일 웹 앱 폴더/ZIP. 선택한 폴더나 ZIP 최상위 또는 단일 상위 폴더에서 앱을 찾음. 폴더 업로드는 파일 5000개·내용 20 MiB 이하
- 자동 대상 선택: AI가 지원 가능한 Local Docker·Cloud Run·AWS ECS Express 중 하나를 소스 근거와 함께 선택. 서버가 대상 가용성, 공개 허용, 근거 일치, 지원 인프라 범위를 검증하고 계획을 이력에 기록
- 인프라 적합성 경계: SQLite 코드·DB 파일, PostgreSQL·MySQL·MongoDB 등의 런타임 의존성·소스·Prisma 설정, 명시적인 `data/`·`uploads/`·`storage/` 파일 쓰기, 워커 스크립트·의존성이 확인되면 현재 모든 대상에서 리소스 생성 전에 차단. 정적 탐지가 모든 상태 저장·비동기 작업을 증명하지는 않음
- 앱 ID: 같은 앱의 배포 이력을 묶는 식별자. AWS는 기존 서비스 URL을 유지하며, 같은 대상에서 진행 중인 배포가 있으면 중복 시작을 거부
- 입력 대기 취소: 환경변수를 기다리는 작업을 취소하면 해당 앱 ID의 다음 배포를 시작할 수 있음
- AI 도구: 프로젝트 파일 읽기, 정확한 텍스트 패치, 실행 설정·Dockerfile 준비, 실제 배포, 로그 조회, 환경변수 요청
- 배포 대상: Local Docker, Google Cloud Run, AWS ECS Express Mode (각 CLI·계정 설정 시 선택 가능)
- 수정 범위: 작업용 복사본의 JS/TS/JSON 및 Python·Ruby·Go 등 주요 텍스트 소스와 기존 Dockerfile. 원본 업로드 소스는 유지
- Dockerfile: 없으면 검증한 실행 설정으로 템플릿 생성, 있으면 업로드한 내용을 보존하고 작업용 복사본에서 AI 수정 가능. 기존 `.dockerignore` 설정을 보존하며 비밀 파일 제외 규칙을 추가
- 완료 조건: 실제 빌드·컨테이너 실행·HTTP 200 확인. AI의 성공 문장만으로 완료되지 않음
- 현재 상태 재검사: 성공 이력에서 버튼을 눌러 소유 리소스·실행 상태·HTTP 200을 다시 확인
- 배포 종료: Local Docker, Cloud Run, AWS ECS Express는 성공 작업의 소유 리소스를 확인한 뒤 종료 가능
- 제한: 배포 시도 최대 3회, AI 도구 호출 최대 24회, 개별 도구 시간 제한 및 루프의 경과 시간 검사
- 미구현: UI의 영속 DB 자동 생성·여러 DB 선택·데이터 이전, GitHub URL 입력, 실행 중 취소, 다중 사용자 격리. 기존 OneDeploy RDS를 명시한 AWS API 업로드와 내부 어댑터의 RDS·마이그레이션·데이터 경로는 실계정 smoke를 통과. UI의 기존 RDS 명시 경로는 실제 Chrome→AWS ECS→RDS HTTP 데이터 smoke를 통과했고 임시 ECS를 종료함. AI 도구 호출은 고정 테스트 응답이며, API 키 미설정으로 실제 모델의 코드 변경·자동 대상 판단은 미검증

AWS에서는 동일한 앱 ID로 다시 배포할 때 소유 중인 기존 ECS Express 서비스를 업데이트하므로 URL을 유지합니다.
AWS의 카나리 전환을 사용하며 실패한 **진행 중** 배포에는 롤백을 요청할 수 있습니다. 완료된 릴리스는
같은 서비스의 선택한 성공 이력의 태스크 정의로 재배포할 수 있습니다. 재확인은 AWS의 완료 결과를 읽어 작업 기록을 복구합니다.
Local Docker와 Cloud Run은
동일한 앱 ID로 다시 배포해도 별도 컨테이너/서비스와 URL을 만듭니다.
`GET /api/applications/<app-id>/releases`에서 해당 앱의 배포 이력을 조회할 수 있습니다.
환경변수 입력 대기 작업은 UI의 **이 작업 취소** 또는 `POST /api/deployments/<job-id>/cancel`로 종료합니다.

Cloud Run 항목은 `gcloud`와 서버 설정이 준비된 환경에서 활성화됩니다. 로그인·권한의 최종 확인은 실제 배포 때 합니다.
AWS 항목은 AWS CLI·Docker CLI·리전이 준비되면 활성화됩니다. 계정 권한·서비스 할당량의 최종 확인은 실제 배포 때 합니다.

## 환경변수·소스·로그

필요한 값이 있으면 진행 중 `waiting_input` 상태로 요청합니다. 값을 입력하면 배포를 이어갑니다.
입력값은 AI에 전송하지 않으며 AI에는 사용 가능한 변수 이름만 알려줍니다. 원문 입력값은 job.json이나
Dockerfile에 저장하지 않습니다. Local Docker 실행에는 권한 0600 임시 env 파일로 전달하고 호출 후 삭제합니다.
Cloud Run과 AWS ECS Express에는 권한 0600 임시 설정 파일을 CLI에 전달하고 호출 후 삭제합니다.
빌드 단계에는 전달하지 않습니다. Docker 엔진과 클라우드 서비스는 실행 환경값을 보관하므로 해당 서비스 접근 권한자는 확인할 수 있습니다.
강제 종료 시 임시 파일이 남을 수 있습니다. 마스킹은 알려진 값 원문·주요 토큰 패턴에 대한 최선의 처리이며
인코딩·변형된 값까지 완전히 탐지하지 않습니다.

코드와 실행 로그 일부는 AI API에 전송됩니다. 직접 만든 신뢰 가능한 앱으로 사용하세요.
ZIP과 폴더의 환경 파일·.git·node_modules는 제외됩니다. 빌드 스크립트는 컨테이너에서 실행하지만 이 프로토타입은
비신뢰 코드를 위한 완전한 격리 플랫폼이 아닙니다. 모델에는 호스트 셸이나 클라우드 자격증명 도구를 제공하지 않습니다.

작업은 `.onedeploy/<job-id>/`에 저장됩니다.

```text
source/       업로드 원본 (수정하지 않음)
work/         AI가 수정하는 복사본
attempt-N/    N번째 실제 빌드 컨텍스트
job.json      상태, 변경 diff, 로그, 접속 URL (환경변수 값 제외)
health.json   최근 상태 확인 20건 (배포 작업 기록과 별도)
```

서버 재시작 후 이력과 환경변수 대기 상태를 복원합니다. 실행 중이던 작업은 `interrupted`로 표시하며
자동 재실행하지 않습니다. 서버 실행 중 상태 디렉터리에 OS 파일 잠금을 유지하므로 같은 디렉터리를
쓰는 두 번째 서버는 시작을 거부합니다. 작업 기록은 권한 0600 임시 파일에 쓴 뒤 원자적으로 교체합니다.
저장 실패 시 메모리상의 작업을 실패로 표시하지만, 이미 생성된 배포 리소스는 남아 있을 수 있습니다.
다중 서버 공유 상태나 분산 작업 실행은 지원하지 않습니다.
성공 이력의 상태는 배포 당시 결과입니다. UI의 **현재 상태 다시 확인**은 해당 Docker 컨테이너 또는
Cloud Run 또는 ECS Express 서비스의 소유 정보 확인 후 HTTP 응답을 다시 검사합니다. AWS는 기록된 HTTPS 주소가
실제 ECS 서비스의 공개 주소와 일치하는지도 확인합니다. `GET /api/jobs/<id>/health`로도
조회할 수 있습니다. 자동·수동 확인 결과는 최근 20건까지 따로 저장하고 UI에 표시합니다.
같은 앱의 새 배포가 진행 중이거나 서비스 종료·롤백 중인 작업은 자동 검사를 건너뜁니다.
검사 결과는 배포 이력의 과거 성공 상태를 변경하지 않으며, 기록 저장에 실패해도 배포 결과는 유지됩니다.

배포된 컨테이너는 서버를 종료해도 유지됩니다. UI의 **이 로컬 배포 종료** 버튼으로 정리하거나,
필요하면 작업 내역에 있는 정확한 이름으로 수동 정리하세요.

```sh
docker rm -f onedeploy-<job-id>-a<attempt>
docker image rm onedeploy/<job-id>-a<attempt>:latest
```

Cloud Run에 성공적으로 배포한 서비스와 업로드 이미지는 서버를 종료해도 유지됩니다.
UI의 **이 Cloud Run 배포 종료** 버튼으로 정리할 수 있습니다.

## 검증

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=. python3 tests/smoke_agent.py
PYTHONPATH=. python3 tests/smoke_agent.py --environment
PYTHONPATH=.:tests python3 tests/smoke_agent.py --python
PYTHONPATH=. python3 tests/smoke_existing_dockerfile.py
```

두 smoke는 **AI의 도구 선택만 테스트용 고정 응답**으로 대체합니다. 업로드 API, 파일 수정,
Docker 빌드·배포, 실패 로그 반환, 수정 후 재시도, 실제 HTTP 응답은 모두 실행합니다.
처음에는 localhost 바인딩으로 실패한 뒤 소스를 수정해 두 번째 배포에서 성공하는지 확인합니다.
테스트가 만든 컨테이너와 앱 이미지는 정리하며 기본 이미지와 빌드 캐시는 남을 수 있습니다.
Python smoke는 `package.json` 없이 원클릭 업로드·작업용 소스 수정·실제 Docker 배포와 종료 API를 검증합니다.
마지막 smoke는 업로드한 Dockerfile과 `.dockerignore`를 보존하면서 실제 이미지를 빌드하고 HTTP 200을 확인합니다.

키와 모델이 설정된 환경에서 실제 AI 판단부터 실행까지 검증하려면:

```sh
PYTHONPATH=. python3 tests/smoke_agent.py --live
```

실제 API 비용과 샘플 소스·로그 전송이 발생합니다. 성공 여부는 모델의 실제 판단에 따라 달라집니다.
Cloud Run 어댑터의 단위 테스트는 실제 계정 없이 실행됩니다. 실제 클라우드 배포는 위 설정을 마친 환경에서
화면의 Cloud Run 대상을 선택해 검증해야 합니다.

이전 정적 분석/계획 API(`/api/analyze`, `/api/deploy/<id>`)는 회귀 테스트를 위해 유지하지만
현재 제품 화면에서는 사용하지 않습니다.

- [현재 설계](docs/design.md)
- [의사결정 기록](docs/decision-log.md)
- [데모 흐름](docs/demo-script.md)
- [발표 초안](docs/talk-script-ko.md)
- [OpenAI 공식 함수 호출 문서](https://developers.openai.com/api/docs/guides/function-calling)
- [Google Cloud 공식 Cloud Run 배포 문서](https://docs.cloud.google.com/run/docs/deploying)
