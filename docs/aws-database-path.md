# AWS 영속 데이터 배포 경로 설계

현재 OneDeploy UI는 일반 배포에서 PostgreSQL·MySQL·MongoDB 등 데이터베이스 의존 앱을 차단한다. AWS ECS Express 대상의 기존 OneDeploy PostgreSQL을 명시하는 예외 경로가 있다. 아래는 AWS에서 **PostgreSQL 한 경로**를 지원하기 위한 설계와 구현 경계다. 별도 DB 생성 명령과 기존 DB를 쓰는 명시적 API 업로드가 있다. 내부 AWS 어댑터와 서버 API의 실제 DB 데이터 경로는 검증했고 UI에 앱 ID 기반 기존 DB 조회·명시적 사용 경로를 추가했다. 조회 API의 실계정 읽기 전용 검증도 통과했다. UI에서 명시적으로 새 DB를 계획·생성하는 경로는 실제 Chrome에서 검증했다. 배포 버튼 하나로 DB까지 자동 생성하거나 여러 DB를 선택하는 기능은 아직 없다. 실제 Chrome에서 기존 DB 조회·업로드·필수 값 재개·AWS 배포·HTTP 데이터 쓰기/읽기·종료까지 고정 AI 응답으로 검증했다.

## 먼저 결정할 경계

- 앱과 DB를 같은 VPC에 두고, ECS 서비스에는 지정한 서브넷과 보안 그룹을 사용한다. DB는 비공개로 만들고 앱 보안 그룹에서 DB 포트로 들어오는 연결만 허용한다. ECS Express는 [네트워크 구성](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ExpressGatewayServiceNetworkConfiguration.html)을 받을 수 있고, RDS는 [VPC·서브넷 그룹·보안 그룹](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html)을 구성해야 한다.
- DB 암호는 앱 배포 명령이나 작업 기록에 넣지 않는다. RDS 관리형 암호를 Secrets Manager에 저장하고, ECS [비밀값 주입](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)에 필요한 실행 역할 권한을 해당 비밀로 제한한다. 비밀 회전 후에는 태스크 재시작이 필요하다.
- 기존 SQLite 파일의 자동 이전은 첫 지원 범위에서 제외한다. 빈 PostgreSQL 스키마를 사용하는 앱만 별도 유형으로 인정하고, 마이그레이션 명령·버전·실패 시 복구 정책이 명확한 경우에만 실행한다. 기존 파일 이전을 지원한다고 표시하지 않는다.
- RDS는 배포 실패나 서비스 종료만을 이유로 즉시 삭제하지 않는다. 데이터가 남는 리소스의 소유권, 보존 기간, 스냅샷 및 명시적 삭제 동작을 서비스 수명주기와 분리한다. CloudFormation의 [RDS DB 인스턴스](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-rds-dbinstance.html)는 보존·삭제 정책과 비용을 별도로 검토해야 한다.

UI의 **새 PostgreSQL RDS 생성 계획 미리보기**는 `POST /api/applications/<앱 ID>/postgres/plan`에 VPC ID와 2~8개 서브넷 ID를 보낸다. 서버는 고정된 AWS 계정과 고정 또는 앱별 검증 서비스 보안 그룹으로 기존 `preflight()`만 수행하며 계정·가용 영역·RDS 주문 가능 구성·Price List의 인스턴스 및 20 GiB 저장소 가격을 반환한다. 계획 API는 DB 스택을 생성하지 않는다. [CloudFormation 스택 목록](https://docs.aws.amazon.com/cli/latest/reference/cloudformation/list-stacks.html)에서 이 앱 ID의 스택 이름이 이미 나타나면 삭제 완료 기록을 포함해 보수적으로 신규 계획을 거부한다. 2026-10-01 인증 HTTP 요청으로 서울 리전의 읽기 전용 계획을 확인했다. UI는 서버가 발급한 15분 만료 계획 ID와 표시된 계정·네트워크·가격을 사용자가 확인한 뒤 별도 생성 버튼을 노출한다. `POST /api/applications/<앱 ID>/postgres/create`는 계획을 다시 읽기 전용으로 검사해 동일할 때만 생성 요청 기록을 디스크에 먼저 저장하고 비동기 CloudFormation 생성을 시작한다. 동일 앱의 중복 생성 요청은 거부한다. 재시작으로 작업이 중단되면 자동 재시도하지 않고 `POST /api/applications/<앱 ID>/postgres/reconcile`에서 계정·스택 ARN·소유 태그와 완료된 DB 구성을 읽기 전용으로 재검증한다. 성공 시 UI가 기존 RDS 연결 입력을 채운다. 실제 프로세스를 AWS 스택 생성 접수 뒤 종료한 드릴에서 재시작 시 `needs_attention` 전환과 생성 완료 후 읽기 전용 `reconcile()` 성공을 확인했다. 2026-10-02 임시 `dbdrill-*` 앱에서 실제 Chrome의 계획·생성 버튼으로 새 RDS 스택을 생성했다. 서버 작업 기록과 AWS `CREATE_COMPLETE`를 대조하고 Chrome에서 완료 상태·기존 DB 입력 자동 채우기를 확인했다. 기존 RDS의 중복 생성 계획 차단·잘못된 계획 ID 차단·합성 중단 기록의 재확인도 실계정에서 읽기 전용으로 검증했다.

## 완료 기준

1. 대상 계정·리전, VPC·서브넷·보안 그룹, DB 엔진·용량·예상 비용 상한을 배포 전에 확인한다.
2. 소유 태그가 있는 비공개 DB와 비밀을 생성하고, 결과 ARN·엔드포인트를 작업 이력에 기록한다. 암호 원문은 기록하지 않는다.
3. 앱 이미지와 DB 접속 설정을 연결하고 마이그레이션을 한 번만 실행한다. 재시작·실패 재시도에도 중복 실행하지 않는다.
4. 서비스의 HTTP 200뿐 아니라 DB 읽기·쓰기·재시작 후 데이터 보존을 검증한다.
5. 앱 업데이트 실패 시 기존 정상 릴리스와 DB를 보존한다. 서비스 종료와 데이터 삭제는 별도의 명시적 작업으로 검증한다.

이 기준을 충족하고 실제 AWS 계정에서 재현하기 전까지는 DB 의존 앱 차단을 유지한다.

앱 전용 ECS 서비스 그룹을 준비하는 독립 CLI `onedeploy.aws_network`도 있다. 기본 모드는 지정 계정·기본 VPC와 동일 앱 스택 중복을 읽기 전용으로 확인한다. `--apply`를 명시하면 `onedeploy-network-<앱 ID>` CloudFormation 스택으로 인바운드 없는 보안 그룹을 만들고, 소유 태그·계정·VPC·허용된 인바운드를 재검증한다. `--inspect`로 생성 후 상태를 다시 읽을 수 있다. 템플릿은 종료 보호를 켜며 자동 삭제하지 않는다. 서버에 `ONEDEPLOY_AWS_SERVICE_SECURITY_GROUP`이 없으면 기존 DB 조회·신규 DB 계획·DB 앱 업로드에서 앱 ID와 VPC의 네트워크 스택을 읽기 전용으로 검증해 출력 그룹을 선택한다. 고정 그룹 ID가 있으면 기존 설정을 사용한다. 실제 AWS에서는 임시 앱 네트워크 스택 생성·앱별 그룹 선택·정리를 통과했다. UI HTTP 생성도 임시 앱에서 실제 Chrome으로 통과했다. 별도 임시 앱에서 새로 만든 DB를 사용하는 ZIP 업로드·SQL 마이그레이션·HTTP 데이터 검증도 한 흐름으로 통과했다.

UI의 **앱 전용 AWS 네트워크 준비**는 `POST /api/applications/<앱 ID>/network/plan`으로 읽기 전용 계정·기본 VPC·중복 스택 검사를 실행한다. 15분 안에 `POST .../network/create`로 계획 ID를 보내야 생성한다. 서버는 작업 기록을 먼저 저장하고 비동기 CloudFormation 생성을 시작하며 완료 시 검증한 스택 ARN과 그룹 ID를 기록한다. `GET .../network/operation`으로 상태를 조회하며 서버 재시작으로 불확실해진 작업은 자동 재실행하지 않는다. `POST .../network/reconcile`은 스택·그룹을 읽기 전용으로 재검증한다. 고정 그룹 환경에서는 앱별 네트워크 계획을 거부한다. 임시 앱의 실제 Chrome UI → AWS 생성·검증·정리를 통과했다. 같은 앱의 네트워크 그룹을 사용한 신규 RDS 생성 연결도 실계정에서 검증했다.

`GET /api/aws/default-network`는 세션 토큰과 고정 AWS 계정 ID를 요구한다. STS로 계정을 확인하고 EC2에서 기본 VPC와 서로 다른 가용 영역의 사용 가능한 기본 서브넷만 반환한다. UI의 **기본 VPC·서브넷 불러오기**가 네트워크 및 새 RDS 계획 필드를 채운다. 실제 서울 리전 계정에서 읽기 전용으로 확인했으며, 이후 네트워크·DB 생성 계획의 소유권·구성 검증을 대체하지 않는다.

```sh
python3 -m onedeploy.aws_network --application <APP_ID> --account <AWS_ACCOUNT_ID> \
  --region ap-northeast-2 --vpc-id <DEFAULT_VPC_ID>
# 실제 그룹을 생성할 때만 --apply 추가; 기존 스택 재검증은 --inspect
```

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
서버에는 `ONEDEPLOY_AWS_ACCOUNT_ID`가 고정돼 있어야 한다. 서비스 그룹은 `ONEDEPLOY_AWS_SERVICE_SECURITY_GROUP`에 고정하거나, 앱 ID·VPC가 일치하는 `onedeploy-network-<앱 ID>` 스택을 미리 만든 뒤 해당 환경 변수를 비워 두어 서버가 검증해 선택하게 한다.
업로드된 앱은 PostgreSQL 단일 엔진으로 확인돼야 하고 `migrations/` SQL 묶음이 있어야 한다.
API는 작업 생성 전에 DB 소유권을 읽기 전용으로 확인하며, DB 리소스를 새로 만들지는 않는다.
헤더를 생략한 일반 업로드에서의 DB 앱 차단은 유지한다. UI는 사용자가 기존 DB와 VPC·서브넷을 명시한 경우에만 위 헤더를 보낸다.
`GET /api/applications/<앱 ID>/postgres`는 세션 토큰과 서버의 AWS 계정·고정 또는 앱별 검증 서비스 보안 그룹을 사용해 해당 앱의 RDS 인스턴스를 찾고 전체 스택·DB 소유권을 다시 확인한다. 응답에는 DB ID·계정·리전·VPC·서브넷·엔진 버전·보존 상태만 포함하고 비밀 ARN·엔드포인트는 포함하지 않는다. UI의 **이 앱의 기존 RDS 조회**가 이 값을 입력란에 채운다. 조회와 업로드 시점은 다를 수 있어 업로드 API가 다시 검사한다. 2026-10-01 실제 AWS 계정에서 인증 HTTP 조회를 읽기 전용으로 통과했다. 앱별 선택 경로는 모의 AWS 테스트만 통과했다.

`GET /api/applications/<앱 ID>/postgres/backups`는 같은 세션 인증과 RDS 소유권 검사를 거쳐 자동 백업 보존 기간, 최근 복원 가능 시점, 삭제 보호·스택 삭제 시 보존 상태와 수동 스냅샷 수·최신 10개를 반환한다. 스냅샷은 [RDS 스냅샷 조회 API](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DescribeDBSnapshots.html)의 `manual` 유형으로 조회하고 응답의 계정·리전·DB ID를 대조한다. UI의 **백업·보호 상태 확인**은 이 읽기 전용 API만 호출한다. 보존 기간·스냅샷 목록 확인은 실제 복원 시험을 대신하지 않는다. `demo-app` 실계정에서는 보존 7일·삭제 보호 켜짐·수동 스냅샷 0개를 확인했다. 수동 스냅샷은 [자동 백업 보존 기간과 별도로 유지](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_CreateSnapshot.html)되므로 이후 생성·폐기 정책과 비용 검토가 필요하다.

`python3 -m onedeploy.postgres_snapshot`은 앱 소유 RDS가 `available`인지와 선택한 스냅샷 ID의 중복 여부를 읽기 전용으로 확인한다. `--apply`가 있을 때만 [CreateDBSnapshot](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_CreateDBSnapshot.html)을 호출하며, 요청에 앱 소유 태그를 포함한다. `--inspect`는 원본 DB가 사라진 뒤에도 고정 AWS 계정·정확한 스냅샷 ARN·원본 DB ID·암호화·태그를 대조할 수 있다. 생성 API는 `creating` 상태를 반환할 수 있어 이를 복구 완료로 간주하지 않는다. 2026-10-01 실계정에서는 계획만 확인했고 생성·복원은 아직 검증하지 않았다.

UI의 **기존 RDS 수동 스냅샷**은 `POST /api/applications/<앱 ID>/snapshots/plan`에서 이름·DB·계정·기존 수동 스냅샷 수와 저장 비용 안내를 먼저 보여준다. 15분 안에 `POST .../snapshots/create`로 계획 ID를 보내면 서버가 같은 계획을 다시 검증하고 생성 작업을 디스크에 먼저 기록한 뒤 비동기로 생성한다. `GET .../snapshots/<스냅샷 ID>/operation`은 로컬 작업 상태를 반환하고 `POST .../reconcile`은 AWS 스냅샷과 소유 태그를 읽기 전용으로 확인한다. 재시작 중인 작업은 자동으로 다시 생성하지 않는다. 2026-10-02 인증 HTTP 계획과 실제 Chrome UI의 읽기 전용 조회는 `demo-app` 실계정에서 통과했다.

2026-10-02 같은 Chrome 경로로 `onedeploy-demo-app-backup-20261002`를 실제 생성했다. 작업 기록의 성공 상태와 AWS 스냅샷의 `available`·암호화·소유 태그를 확인했다. 원본 RDS는 `available`로 남아 있다. 이 스냅샷은 계속 보존되며 실제 청구액은 확인하지 않았다. 복원 검증은 별도 작업이다.

`python3 -m onedeploy.postgres_restore`는 [RDS가 스냅샷을 새 DB 인스턴스로 복원한다는 동작](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_RestoreFromSnapshot.html)에 맞춰 원본과 다른 대상 ID를 요구한다. 앱 소유 스냅샷의 상태·암호화·VPC·엔진·버전·용량을 원본 DB와 대조하고 대상 ID 중복과 현재 기본 용량 가격을 읽는다. 복원 대상의 `onedeploy.postgres_restore_network`는 별도 보안 그룹을 계획·생성·검증한다. [EC2가 새 그룹에 추가하는 기본 아웃바운드 규칙](https://docs.aws.amazon.com/vpc/latest/userguide/creating-security-groups.html)도 제거해 인바운드·아웃바운드가 모두 비었는지 확인한다. `--delete <GROUP_ID>`는 소유권·미사용·다른 그룹의 참조가 없는 경우에만 실행한다. `onedeploy.postgres_restore_instance`는 같은 대상의 격리 그룹과 복원 계획을 재검증한 뒤 DB를 생성하고, 계정·VPC·보안 그룹·암호화·태그를 조회하며, 소유 DB가 `available`일 때만 명시적으로 삭제한다. 실제 서울 리전에서 이 생성·검증·삭제 경로를 통과했다.

`onedeploy.postgres_restore_probe_network`는 일회성 ECS 검사 작업용 보안 그룹을 따로 만든다. 기본 아웃바운드를 제거하고 복원 DB 그룹의 TCP 5432 및 AWS API 연결용 HTTPS 443만 허용한다. 복원 DB에는 해당 작업 그룹에서 오는 TCP 5432만 잠시 허용한다. 정리 전에 작업 네트워크 인터페이스가 남아 있으면 중단하고, DB 인바운드를 먼저 닫은 다음 검사 그룹을 삭제한다. 서울 리전에서 실제 복원 DB의 ECS SQL 검사에 이 연결을 사용하고 검사가 끝난 뒤 닫았다.

`postgres_restore_drill_operations`는 복원 계획과 보안 그룹 계획을 읽기 전용으로 통과한 뒤 대상별 로컬 기록을 먼저 동기화한다. 격리 그룹 생성과 DB 복원 요청의 전후 단계를 기록하며 같은 대상의 재시작을 막는다. `--reconcile`은 이름·태그로 소유 그룹을 찾고 DB ARN·소유 태그·격리 구성을 읽기 전용으로 확인한다. 응답이 끊겨도 생성 API를 다시 호출하지 않는다. 실제 복원 DB 생성·재확인 후, `--finalize`로 SQL 성공·ECS 정리·복원 DB와 임시 그룹 부재를 확인해 작업 기록을 `cleaned`로 마감했다. RDS가 생성 중 `configuring-enhanced-monitoring`을 반환한 실제 사례를 진행 상태로 처리한다.

`postgres_restore_verifier`는 기존 검증된 마이그레이션 번들의 파일명·SHA-256만 별도 이미지에 포함한다. Node 검사는 `BEGIN READ ONLY`에서 복원 DB의 `onedeploy_schema_migrations` 행을 정확히 비교한다. 선택적 32자리 검사 ID가 있으면 `onedeploy_probe_migrated`의 해당 행도 조회한다. 결과에는 검사 개수와 성공 여부만 남기고 행 내용·비밀번호는 출력하지 않는다. 소스 스냅샷 이후 비밀번호가 변경됐을 수 있으므로, 실제 ECS 작업에서 복원 DB 인증 성공을 확인해야 한다. 이미지 문맥과 SQL 로직은 단위 테스트를 통과했다.

`postgres_restore_credentials`는 원본 RDS의 소유권·관리형 비밀 ARN과 소유 스냅샷 시각을 확인하고, 비밀 값 없이 Secrets Manager 버전 메타데이터만 읽는다. `AWSCURRENT`가 정확히 하나이고 버전 생성 시각이 스냅샷보다 앞설 때만 [ECS의 버전 ID 고정 비밀 참조](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)를 반환한다. 서울 리전의 보존 스냅샷에서 읽기 전용으로 통과했다. 이 시각 비교는 비밀번호 일치의 증명이 아니므로 실제 인증·SQL 성공이 복원 검증의 필수 조건이다.

`postgres_restore_task`의 사전 계획은 검사 연결이 열린 보안 그룹 쌍, 소유 태그를 가진 `available` 복원 DB와 엔드포인트, 원본 RDS 스택의 제한된 ECS 실행 역할, 고정 비밀 버전, 이미지·비밀 다운로드가 가능한 공개 서브넷, 14일 보존 로그 그룹을 대조한다. 실행기는 검증 manifest만 담은 이미지를 빌드해 ECR digest를 확인하고, 그 digest와 비밀 버전을 고정한 단일 Fargate 작업을 실행한다. 종료 코드 0과 CloudWatch의 SQL 성공 메시지를 모두 확인해야 통과한다. 성공 시에만 작업 정의와 이미지 태그를 정리한다. `postgres_restore_task_operations`는 대상당 한 번만 시작하도록 로컬 기록을 AWS 변경 전에 동기화하고 단계마다 이미지 digest·정의 ARN·작업 ARN을 저장한다. 중단 후 `--reconcile`은 STS 계정과 AWS 자원·기록된 SQL 결과를 읽기 전용으로 재확인한다. 작업 ARN을 받기 전에 실행 응답이 끊기면 자동 재시작하지 않고 수동 조사를 요구한다. 실제 복원 DB에서 마이그레이션 원장 1건의 이름·SHA-256을 확인했고 작업 정의·이미지 정리도 재확인했다. 두 번째 드릴은 스냅샷 전 기록한 고유 표식 행의 값을 복원 DB에서 일치 확인했다.

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

## 명시적 RDS 폐기

`onedeploy.postgres_retirement`와 서버의 `database-retirement-operations/<앱 ID>.json`은 일반 앱의 명시적 폐기를 담당한다. 읽기 전용 계획은 원본 DB·스택의 소유권과 삭제 보호, 현재 ECS 서비스·실행 중인 태스크의 DB 비밀 미사용을 확인한다. 서버 API는 15분짜리 계획 토큰과 정확한 `onedeploy-<앱 ID>` 입력을 요구하고, 작업을 디스크에 먼저 동기화한 뒤 비동기로 실행한다. CLI는 기본값이 계획이며, 실제 실행에는 `--apply`, 새 로컬 작업 기록 경로, 정확한 DB ID가 모두 필요하다.

실행 순서는 고유한 최종 수동 스냅샷 요청 → `available` 및 소유 태그·암호화 확인 → DB 소유권·ECS 사용자 재검사 → 삭제 보호 해제와 재확인 → ECS 사용자·스냅샷 마지막 확인 → DB 삭제·완료 대기 → 최종 스냅샷 재확인 → 스택 종료 보호 해제·삭제·완료 대기 → 최종 스냅샷 재확인이다. 기존 수동 스냅샷과 앱 네트워크는 보존된다. 자동 백업은 DB와 함께 삭제되며 수동 스냅샷 저장 비용은 계속 발생할 수 있다.

기록의 `stage`는 다음 AWS 변경 **직전**에도 저장된다. `creating_final_snapshot`, `removing_db_protection`, `deleting_database`, `removing_stack_protection`, `deleting_stack`에서 중단되면 요청이 AWS에 접수됐는지 단정할 수 없다. 삭제 보호 해제 단계에서 일반 오류가 나면 보호 복구를 시도해 재확인하지만 프로세스 강제 종료에는 실행되지 않는다. 서버 재시작은 `running`을 `needs_attention`으로 바꾸고 재실행하지 않는다. 그 경우 기록의 계정·DB ARN·스택 ARN·스냅샷 ID로 실제 AWS 자원을 수동 대조한다. 특히 보호 해제 뒤 강제 종료됐다면 원본 DB의 삭제 보호 상태를 확인해야 한다. 성공 기록을 포함한 폐기 기록이 있는 앱 ID의 AWS 재배포는 막는다.
