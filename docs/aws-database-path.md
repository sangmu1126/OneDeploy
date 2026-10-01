# AWS 영속 데이터 배포 경로 설계

현재 OneDeploy UI는 일반 배포에서 PostgreSQL·MySQL·MongoDB 등 데이터베이스 의존 앱을 차단한다. AWS ECS Express 대상의 기존 OneDeploy PostgreSQL을 명시하는 예외 경로가 있다. 아래는 AWS에서 **PostgreSQL 한 경로**를 지원하기 위한 설계와 구현 경계다. 별도 DB 생성 명령과 기존 DB를 쓰는 명시적 API 업로드가 있다. 내부 AWS 어댑터와 서버 API의 실제 DB 데이터 경로는 검증했고 UI에 앱 ID 기반 기존 DB 조회·명시적 사용 경로를 추가했다. 조회 API의 실계정 읽기 전용 검증도 통과했다. UI의 DB 자동 생성·여러 DB 선택은 아직 없다. 실제 Chrome에서 기존 DB 조회·업로드·필수 값 재개·AWS 배포·HTTP 데이터 쓰기/읽기·종료까지 고정 AI 응답으로 검증했다.

## 먼저 결정할 경계

- 앱과 DB를 같은 VPC에 두고, ECS 서비스에는 지정한 서브넷과 보안 그룹을 사용한다. DB는 비공개로 만들고 앱 보안 그룹에서 DB 포트로 들어오는 연결만 허용한다. ECS Express는 [네트워크 구성](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ExpressGatewayServiceNetworkConfiguration.html)을 받을 수 있고, RDS는 [VPC·서브넷 그룹·보안 그룹](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html)을 구성해야 한다.
- DB 암호는 앱 배포 명령이나 작업 기록에 넣지 않는다. RDS 관리형 암호를 Secrets Manager에 저장하고, ECS [비밀값 주입](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)에 필요한 실행 역할 권한을 해당 비밀로 제한한다. 비밀 회전 후에는 태스크 재시작이 필요하다.
- 기존 SQLite 파일의 자동 이전은 첫 지원 범위에서 제외한다. 빈 PostgreSQL 스키마를 사용하는 앱만 별도 유형으로 인정하고, 마이그레이션 명령·버전·실패 시 복구 정책이 명확한 경우에만 실행한다. 기존 파일 이전을 지원한다고 표시하지 않는다.
- RDS는 배포 실패나 서비스 종료만을 이유로 즉시 삭제하지 않는다. 데이터가 남는 리소스의 소유권, 보존 기간, 스냅샷 및 명시적 삭제 동작을 서비스 수명주기와 분리한다. CloudFormation의 [RDS DB 인스턴스](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-rds-dbinstance.html)는 보존·삭제 정책과 비용을 별도로 검토해야 한다.

## 완료 기준

1. 대상 계정·리전, VPC·서브넷·보안 그룹, DB 엔진·용량·예상 비용 상한을 배포 전에 확인한다.
2. 소유 태그가 있는 비공개 DB와 비밀을 생성하고, 결과 ARN·엔드포인트를 작업 이력에 기록한다. 암호 원문은 기록하지 않는다.
3. 앱 이미지와 DB 접속 설정을 연결하고 마이그레이션을 한 번만 실행한다. 재시작·실패 재시도에도 중복 실행하지 않는다.
4. 서비스의 HTTP 200뿐 아니라 DB 읽기·쓰기·재시작 후 데이터 보존을 검증한다.
5. 앱 업데이트 실패 시 기존 정상 릴리스와 DB를 보존한다. 서비스 종료와 데이터 삭제는 별도의 명시적 작업으로 검증한다.

이 기준을 충족하고 실제 AWS 계정에서 재현하기 전까지는 DB 의존 앱 차단을 유지한다.

현재 구현은 기본 VPC의 추가 ECS 서비스 보안 그룹 하나를 선택하고 실제 적용을
검증한다. 새 그룹은 인바운드 규칙이 없어야 한다. ECS Express가 배포 후 로드 밸런서
보안 그룹에서 앱 포트로 들어오는 규칙 하나를 추가할 수 있으므로, 이후 점검에서는
동일 VPC·계정의 단일 보안 그룹을 출처로 하는 단일 포트 규칙만 허용한다.
CIDR 공개 규칙이나 다른 인바운드 규칙은 거부한다. PostgreSQL에는
`onedeploy-managed=true`와 해당 `onedeploy-app=<앱 ID>` 태그가 있는 **앱 전용**
서비스 보안 그룹이 필요하다. 다른 서비스가 쓰는 그룹을 재사용하면 그 서비스에도
DB 접근 권한을 주므로 DB 생성 전과 기존 DB 재검사 때 모두 소유 태그를 확인한다.
별도의 `onedeploy.postgres` 명령은 지정한 기본 VPC의 두 가용 영역을 확인하고,
PostgreSQL 인스턴스·DB 보안 그룹·서브넷 그룹을 **명시적 `--apply`일 때만** 새 스택으로 생성한다.
사전 점검은 해당 리전의 기본 PostgreSQL 엔진 버전을 조회하고 지정한 가용 영역 모두에서
`db.t4g.micro`·암호화된 `gp3` 20 GiB 구성이 주문 가능한지 확인한다
([RDS 주문 가능 옵션](https://docs.aws.amazon.com/cli/latest/reference/rds/describe-orderable-db-instance-options.html)).
생성 요청에는 확인한 엔진 버전을 고정하고, [RDS Extended Support 기본 등록](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-rds-dbinstance.html)을
해제해 표준 지원 종료 후 추가 요금이 붙는 구성을 거부한다. 사전 점검은 AWS
[Price List Query API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-price-list-query-api.html)에서
해당 리전의 단일 AZ 인스턴스 시간당 요금과 `gp3` 스토리지 GiB·월 요금을 각각 정확한 SKU로 조회하고,
730시간·20GiB 기준 용량 비용만 USD로 표시한다. 가격 조회 권한이 없거나 SKU·단위가 불명확하면
DB 생성을 시작하지 않는다. 백업 초과분·추가 IOPS/처리량·전송·비밀·로그·ECS·세금 등은 제외하므로
이 수치는 총액이나 예산 상한이 아니다. 실제 생성 전 전체 예상 비용을 별도로 확인해야 한다.
DB 보안 그룹은 지정한 서비스 보안 그룹에서 포트 5432로 오는 연결만 허용한다. RDS 암호는 관리형
Secrets Manager 비밀로 두고 원문을 출력하지 않는다. 같은 스택에 DB 전용 ECS 실행 역할도 생성하며,
기본 ECS 실행 권한에 더해 해당 RDS 비밀 ARN 하나의 `secretsmanager:GetSecretValue`만 허용한다
([AWS 태스크 실행 역할 문서](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html)).
생성 결과와 `--inspect` 출력에는 이 역할의 ARN만 기록한다. AWS 배포 어댑터의 명시적
`postgres=PostgresRequest(...)` 경로는 기존 DB를 읽기 전용으로 재검증한 뒤 역할과
Secrets Manager의 `username`·`password` JSON 키를 ECS 환경에 참조로 연결한다.
`PGHOST`·`PGPORT`·`PGDATABASE`·`PGSSLMODE`도 설정하고 실제 ECS 리비전의 설정을 대조한다.
DB 재조회 시 IAM 역할의 소유 태그·ECS 태스크 신뢰 정책·관리형/인라인 정책을 읽기 전용으로
대조하며, 다른 권한이 붙거나 비밀 ARN 범위가 넓어지면 연결을 거부한다.
업로드 UI에서 AWS ECS Express를 선택하면 기존 OneDeploy PostgreSQL을 명시할 수 있다.
실패 복구·데이터 수명주기는 아직 남아 있다. 브라우저 실제 AWS 경로는 고정 AI 응답으로 검증했다. 스택 삭제나 교체에도 DB를 보존하고 삭제 보호를
켜고 스택 종료 보호도 적용하므로, 앱 배포 실패·서비스 종료가 데이터를 지우지 않는다. 이 설정은 계속 비용이 발생할 수 있으며,
DB 폐기는 별도 스냅샷·보호 해제·소유권 검증 절차가 필요하다.

내부 배포 도구에는 명시적 `postgres_request` 입력 경로가 있다. 이 경로는 소스에서
PostgreSQL 엔진만 확인된 AWS 앱에만 적용하고, `PGHOST`·`PGPASSWORD` 등 관리형 변수는
사용자에게 다시 요청하지 않고 검증된 DB 바인딩을 AWS 어댑터에 전달한다.
MySQL·MongoDB·혼합/불명 엔진과 `DATABASE_URL` 접속 방식은 이 경로에서 차단한다.
서버 업로드 API에는 **기존에 생성된 OneDeploy RDS 스택**을 사용하는 명시적 옵션만 노출한다.
`POST /api/deployments`에서 일반 세션 토큰과 앱 ZIP/폴더 업로드 외에 다음 헤더가 필요하다.
`X-Deploy-Target: aws-ecs-express`, `X-Public-Access: true`,
`X-Application-Id: <DB_APP_ID>`, `X-Postgres-Existing: true`,
`X-Postgres-Vpc-Id: <DEFAULT_VPC_ID>`,
`X-Postgres-Subnet-Ids: <SUBNET_A_ID>,<SUBNET_B_ID>`.
서버에는 `ONEDEPLOY_AWS_ACCOUNT_ID`와 `ONEDEPLOY_AWS_SERVICE_SECURITY_GROUP`도 고정돼 있어야 한다.
업로드된 앱은 PostgreSQL 단일 엔진으로 확인돼야 하고 `migrations/` SQL 묶음이 있어야 한다.
API는 작업 생성 전에 DB 소유권을 읽기 전용으로 확인하며, DB 리소스를 새로 만들지는 않는다.
헤더를 생략한 일반 업로드에서의 DB 앱 차단은 유지한다. UI는 사용자가 기존 DB와 VPC·서브넷을 명시한 경우에만 위 헤더를 보낸다.
`GET /api/applications/<앱 ID>/postgres`는 세션 토큰과 서버의 AWS 계정·앱 전용 보안 그룹을 사용해 해당 앱의 RDS 인스턴스를 찾고 전체 스택·DB 소유권을 다시 확인한다. 응답에는 DB ID·계정·리전·VPC·서브넷·엔진 버전·보존 상태만 포함하고 비밀 ARN·엔드포인트는 포함하지 않는다. UI의 **이 앱의 기존 RDS 조회**가 이 값을 입력란에 채운다. 조회와 업로드 시점은 다를 수 있어 업로드 API가 다시 검사한다. 2026-10-01 실제 AWS 계정에서 인증 HTTP 조회를 읽기 전용으로 통과했다.

마이그레이션 실행기의 로컬 구성도 준비했다. 앱의 `migrations/0001_name.sql` 형식 SQL 파일을
최대 32개·파일당 64 KiB로 검증하고, 파일명과 SHA-256을 고정한 별도 Docker 빌드 문맥을 만든다.
실행기는 PostgreSQL의 트랜잭션별 advisory lock과 `onedeploy_schema_migrations` 이력으로
같은 파일의 중복 적용을 건너뛰고, 적용한 파일의 체크섬이 바뀌면 실패한다.
파일 안의 트랜잭션 제어문은 허용하지 않는다. 내부 `postgres_request` 경로에서는
DB 스택의 14일 보존 CloudWatch 로그 그룹과 공개 서브넷을 확인한 뒤 일회성
Fargate 태스크에서 이 실행기를 돌린다. ECR에 업로드한 이미지의 계정·저장소·태그와
SHA-256 digest를 재조회하고 태스크 정의에는 변경 불가능한 digest 참조를 사용한다
([ECR 이미지 조회](https://docs.aws.amazon.com/cli/latest/reference/ecr/describe-images.html),
[ECS 컨테이너 이미지 형식](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ContainerDefinition.html)).
마이그레이터 Dockerfile도 Node 22 Alpine 베이스 이미지 digest와 `pg` 의존성 lockfile을
고정하고 `npm ci`로 설치한다. 베이스 이미지·의존성 갱신은 lockfile과 digest를 함께 검토해야 한다.
실계정 첫 실행에서는 Node가 RDS 서버 인증서의 CA를 신뢰하지 못해 실패했다.
[AWS 공식 RDS CA 번들](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html)을
체크섬으로 고정해 마이그레이터와 DB 앱 이미지에 넣고 `NODE_EXTRA_CA_CERTS`로 Node의
서버 인증 검증에 사용한다. 번들 교체 시 체크섬을 검토·갱신해야 한다.
태스크 종료 코드가 0일 때만 웹 서비스 배포를 계속하며,
성공 후 마이그레이션 태스크 정의와 임시 ECR 이미지 태그를 정리한다.
태스크 정의·태스크 ARN·이미지·SQL 묶음 체크섬과 성공 결과를 작업 기록에 저장한다.
실행 결과가 불확실하면 해당 태스크·정의·이미지를 남기고 자동 재시도 없이 수동 확인을 요구한다.
스키마 적용 뒤 웹 서비스 배포에 실패해도 마이그레이션 성공 기록은 남는다.
**내부 AWS 어댑터와 서버 API의 DB 마이그레이션·데이터 경로는 2026-10-01 실계정 smoke를 통과했다. API 옵션은 기존 DB를 명시한 경우에만 작동한다.**
마이그레이션은 기존 앱 버전과 호환되는 SQL이어야 하며, DB 스키마 변경 자체를 롤백하지 않는다.

결과 확인이 불확실한 태스크는 기록된 `aws_migration_task_arn`과
`aws_migration_task_definition_arn`으로 다음 읽기 전용 명령을 실행한다. 먼저 STS 계정을
고정한 뒤 ECS 태스크의 클러스터·Fargate 실행 방식·정의·소유 태그·컨테이너 종료 코드를 대조한다.
결과는 `running`, `succeeded`, `failed`, `unknown` 중 하나다. `unknown`은 성공으로 취급하지
않으며, 이 명령은 작업 기록을 수정하거나 서비스를 재배포하지 않는다. ECS의 [종료된 태스크 조회](https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-tasks.html)는
최소 1시간만 보장되므로 오래된 결과는 CloudWatch 로그와 DB 마이그레이션 이력을 별도로 확인해야 한다.

```sh
python3 -m onedeploy.aws_migrations --application demo-app \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --attempt <DEPLOYMENT_ATTEMPT_ID> \
  --task-arn <AWS_MIGRATION_TASK_ARN> \
  --task-definition-arn <AWS_MIGRATION_TASK_DEFINITION_ARN>
```

```sh
python3 -m onedeploy.postgres --application demo-app --account <AWS_ACCOUNT_ID> \
  --region ap-northeast-2 --vpc-id <DEFAULT_VPC_ID> \
  --subnet-id <SUBNET_A_ID> --subnet-id <SUBNET_B_ID> \
  --service-security-group <RESTRICTED_SERVICE_GROUP_ID>
# 실제 생성할 때만 마지막에 --apply 추가
# 기존 DB의 소유권·암호화·비공개 설정·서브넷·보안 그룹을 읽기 전용으로 재확인하려면 --inspect 추가
```

이 명령은 **DB 리소스만 준비**한다. 어댑터의 내부 옵션 경로에는 앱 접속 설정과
마이그레이션과 앱 업데이트 뒤 데이터 보존은 실제 AWS에서 검증했다. 앱 종료와 DB의
독립적인 수명주기 및 실패 복구 검증은 아직 남아 있다.
`--apply`만 실행해도 OneDeploy 제품 UI에서
DB 의존 앱 차단이 자동 해제되지는 않는다. 기존 RDS 사용을 UI에서 명시해야 한다. `--inspect`는 지정한 AWS 계정과 스택 소유권을 확인하고,
RDS의 암호화·삭제 보호·비공개 엔드포인트·서브넷, DB 보안 그룹의 5432 인바운드 범위를 대조한다.
2026-10-01 실제 AWS 계정에서 `demo-app` 스택의 생성·점검을 통과했다. 생성된 RDS는
비공개·암호화·삭제 보호 상태로 보존돼 있으며 비용이 계속 발생한다.

기존 DB를 이용한 라이브 데이터 경로 smoke는 별도로 준비했다. 기본 실행은 `--inspect`와 같은
읽기 전용 검증만 한다. `--apply`를 지정하면 SQL 마이그레이션이 smoke용 테이블을 만들고,
인증 헤더가 필요한 임시 Node.js 앱을 ECS에 배포한다. 앱은 테이블을 직접 생성하지 않고
PostgreSQL에 임의 ID를 쓰고 읽는다. 같은 서비스의 새 이미지 리비전을 배포할 때
마이그레이션을 다시 호출해 체크섬 기반 중복 실행 방지를 확인한 뒤 데이터를 다시 읽는다.
성공 시 검증 행을 삭제하고 임시 ECS 서비스와 이미지 태그를 정리한다. RDS·비밀과 smoke용 테이블은 보존된다.
2026-10-01 실제 AWS 계정에서 이 smoke를 실행해 통과했다. v1·v2 각각의 SQL
마이그레이션, v1 쓰기/읽기, 같은 URL의 v2에서 기존 행 읽기 및 검증 행 삭제를 확인했다.
임시 ECS 서비스와 두 앱 이미지 태그는 정리했고 RDS·비밀은 보존했다.

```sh
PYTHONPATH=. python3 tests/smoke_aws_postgres.py --application demo-app \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --vpc-id <DEFAULT_VPC_ID> \
  --subnet-id <SUBNET_A_ID> --subnet-id <SUBNET_B_ID> \
  --service-security-group <RESTRICTED_SERVICE_GROUP_ID>
# ECS와 ECR을 실제 사용하고 비용을 발생시킬 때만 --apply 추가
```
