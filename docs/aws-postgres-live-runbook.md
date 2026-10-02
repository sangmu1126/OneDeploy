# AWS PostgreSQL 실계정 검증 절차

## 2026-10-03 생성 실패·CloudFormation 롤백 드릴

`tests.smoke_aws_postgres_rollback_drill --apply`는 임시 `dbdrill-5f443d7b` 앱의 전용 네트워크를 만든 뒤, 제품 `PostgresOperations.plan()`·`start()` 경로로 생성 요청을 기록했다. 드릴은 **시험 프로세스에서만** RDS가 없는 CloudFormation 템플릿을 주입했다. 템플릿에는 신호를 보내지 않는 WaitCondition과 그 핸들만 있으며, 실제 AWS `ValidateTemplate`을 통과했다. 신호 대기 시간 초과로 스택이 `ROLLBACK_COMPLETE`가 됐다. [AWS WaitCondition 동작](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-waitcondition.html)

제품 생성 작업은 자동 재시도 없이 `needs_attention`으로 전환됐고, 읽기 전용 `reconcile()`은 앱 소유 스택의 `ROLLBACK_COMPLETE`를 메시지에 표시하며 DB 성공으로 오인하지 않았다. 드릴은 계정·태그·종료 보호·스택 리소스가 시험용 두 개뿐임·앱 DB와 수동 스냅샷 부재를 확인한 후에만 실패 스택의 종료 보호를 해제하고 삭제했다. 전용 네트워크도 정리했다. 후속 AWS 조회에서 임시 활성 스택·DB·수동 스냅샷은 없고, 기존 `onedeploy-demo-app`은 `available`·삭제 보호 켜짐, 기존 백업 스냅샷은 `available`이다. 실제 청구액은 확인하지 않았다.

```sh
python3 -m tests.smoke_aws_postgres_rollback_drill \
  --apply --account <AWS_ACCOUNT_ID> --region ap-northeast-2
```

`--apply`가 없으면 읽기 전용 네트워크 사전 점검만 한다. 이 드릴은 AWS가 **접수한 생성 요청의 롤백 결과를 판별하고 시험 자원을 정리하는 경로**를 검증한다. 일반 제품 UI에서 실패 스택을 직접 정리하거나 같은 앱 ID로 안전하게 새 생성 시도를 시작하는 흐름은 아직 구현하지 않았다. 제품 작업 기록은 실패 후에도 남아 중복 생성을 차단한다.

## 2026-10-03 제품 UI에서 임시 RDS 폐기

`tests.smoke_aws_postgres_create_browser --apply --retire-through-ui`로 임시 `dbdrill-e8de6dc2` 앱의 전용 네트워크와 RDS를 실제 Chrome에서 생성했다. 브라우저의 생성 완료 상태, 서버 기록, AWS 리소스 식별자를 대조한 뒤 **PostgreSQL RDS 폐기** 계획을 열었다. 잘못된 DB ID는 UI에서 거부됐고, 정확한 `onedeploy-dbdrill-e8de6dc2` 입력으로 폐기 작업을 접수했다. 제품 작업 기록은 최종 암호화 수동 스냅샷 검증 → DB 삭제 → RDS 스택 `DELETE_COMPLETE`까지 `succeeded`/`stack_deleted`로 끝났다. 새 Chrome 연결에서 폐기 완료와 생성 작업의 `retired` 표시, 기존 DB 사용 체크 해제를 확인했다.

시험 도구의 첫 실행은 제품 폐기 성공 후 정리 단계에서 생성 작업 기록에 없는 `stack_id`를 읽어 종료 코드 1을 반환했다. 폐기 작업의 로컬 기록에 있는 스택 ID를 사용하도록 수정한 뒤, 앱 소유·`available` 스냅샷과 삭제 완료 스택·DB 부재를 확인해 시험용 최종 스냅샷과 전용 네트워크를 별도로 정리했다. 후속 AWS 조회에서 임시 DB·수동 스냅샷·관리형 비밀은 없고, 기존 `onedeploy-demo-app`은 `available`·삭제 보호 켜짐, 기존 백업 스냅샷은 `available`이다. 로컬 기록은 `.onedeploy/browser-db-drills/dbdrill-e8de6dc2/`에 남겼다. 이 실행의 실제 청구액은 확인하지 않았다.

```sh
python3 -m tests.smoke_aws_postgres_create_browser \
  --apply --retire-through-ui --account <AWS_ACCOUNT_ID> --region ap-northeast-2
```

`--apply`가 없으면 읽기 전용 사전 점검만 한다. 이 드릴은 새 임시 RDS와 최종 스냅샷을 생성하고, 제품 UI에서 폐기한 뒤 시험 자원을 정리한다. 폐기 상태가 불확실하면 시험 도구는 DB·스냅샷을 자동으로 삭제하지 않는다.

## 2026-10-02 생성 중 작업 프로세스 종료·재시작 복구

`tests.smoke_aws_postgres_restart_drill --apply`는 임시 `dbdrill-4f956cb2` 앱의 네트워크를 만들고 별도 프로세스에서 DB 생성 계획·요청을 기록했다. AWS CloudFormation이 앱 소유 `CREATE_IN_PROGRESS` 스택을 접수한 것을 확인한 직후 생성 작업 프로세스와 그 하위 명령을 종료했다. 새 `PostgresOperations` 인스턴스는 디스크의 `running` 기록을 `needs_attention`으로 바꾸고 생성 요청을 자동 반복하지 않았다. AWS 스택이 완료된 뒤 새 인스턴스의 `reconcile()`이 계정·스택 소유 태그·실제 RDS 구성을 읽기 전용으로 대조해 기록을 `succeeded`로 복구했다.

이후 전용 임시 DB 정리기로 DB와 RDS 스택을 삭제하고 앱 네트워크 스택을 종료했다. 최종 조회에서 DB·두 스택은 이름으로 찾을 수 없고 앱 태그 보안 그룹·수동 스냅샷·관리형 비밀·마이그레이션 로그 그룹은 `[]`였다. 원본 `onedeploy-demo-app`은 `available`·삭제 보호 켜짐이다. Git 제외 로컬 기록은 `.onedeploy/postgres-restart-drills/dbdrill-4f956cb2/`에 남겼다. 이 드릴은 성공적으로 접수된 생성 요청의 **프로세스 중단 후 복구**를 검증하며, AWS 생성 실패나 롤백 경로 자체는 검증하지 않는다. 실제 청구액은 확인하지 않았다.

```sh
python3 -m tests.smoke_aws_postgres_restart_drill \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2
# 기본은 읽기 전용 네트워크 사전 점검. 임시 RDS 생성·프로세스 중단·정리에는 --apply 추가.
```

## 2026-10-02 신규 RDS부터 앱 배포까지 브라우저 통합 검증

`tests.smoke_aws_postgres_create_browser --apply --deploy-app`로 임시 `dbdrill-bde4c148` 앱의 네트워크·PostgreSQL RDS를 실제 Chrome에서 계획·생성했다. RDS `CREATE_COMPLETE`와 서버 생성 기록을 대조한 뒤 같은 앱 ID의 기존 DB 조회로 연결 입력을 채웠다. 브라우저는 SQL 마이그레이션을 포함한 샘플 ZIP을 업로드하고 `PROBE_KEY` 입력 대기에서 값을 제공해 배포를 재개했다. 테스트용 고정 AI 도구 응답을 사용했으며 실제 모델 판단은 포함하지 않았다.

배포 작업 `b93653fa3bd94258`은 첫 시도에 성공했다. 작업 결과의 DB ID가 방금 생성한 RDS와 일치하고, 마이그레이션 작업 정리가 완료됐으며, 상태 API가 정상임을 확인했다. 공개 HTTPS 엔드포인트에서 고유 행의 쓰기·읽기·삭제를 확인했고 Chrome 작업 내역도 `배포 완료`를 표시했다. 종료 API로 ECS 서비스를 `INACTIVE`로 만들고 해당 ECR 이미지 태그 삭제를 확인한 뒤에만 DB 정리를 시작했다.

임시 DB 삭제, RDS/네트워크 스택 삭제가 완료됐다. 후속 AWS 조회에서 임시 DB·스택·이미지 태그는 찾을 수 없고, 앱 태그 보안 그룹·수동 스냅샷·관리형 비밀·마이그레이션 로그 그룹 목록은 `[]`였다. 기존 `onedeploy-demo-app`은 `available`·삭제 보호 켜짐이다. 로컬 생성·배포·정리 기록은 Git에서 제외한 `.onedeploy/browser-db-drills/dbdrill-bde4c148/`에 남겼다. 실제 청구액은 확인하지 않았다.

```sh
python3 -m tests.smoke_aws_postgres_create_browser \
  --apply --deploy-app --account <AWS_ACCOUNT_ID> --region ap-northeast-2
```

## 2026-10-02 신규 RDS 브라우저 생성·임시 리소스 정리

`tests.smoke_aws_postgres_create_browser --apply`로 임시 `dbdrill-9e74eba8` 앱의 네트워크와 새 PostgreSQL RDS를 실제 Chrome에서 계획·생성했다. 브라우저는 기본 VPC/서브넷 자동 입력, 두 개의 읽기 전용 계획과 별도 생성 버튼, RDS 생성 요청의 `running` 상태를 확인했다. 서버의 디스크 작업 기록과 AWS 스택 `CREATE_COMPLETE`를 대조했다. 최초 드라이버가 생성 완료까지 같은 Chrome 디버그 연결로 계속 폴링하다 30초 응답 제한에 걸렸지만, 같은 실행 중인 Chrome에 다시 연결해 UI의 `succeeded`와 기존 DB 연결 VPC/서브넷 자동 입력을 확인했다. 이후 드라이버는 생성 요청과 완료 재조회를 별도 실행 단계로 분리했다.

임시 DB 정리기는 `dbdrill-<8 hex>` 앱만 허용한다. 삭제 전에 전체 DB 소유권, 수동 스냅샷 부재, 실행 중 ECS 서비스·태스크의 해당 비밀 미사용, 스택 상태와 종료 보호를 검사한다. 로컬 작업 기록을 먼저 만들고 DB 삭제 보호 해제 → DB 삭제 확인 → 스택 종료 보호 해제·삭제 순서로 진행한다. 실제 실행에서 DB `onedeploy-dbdrill-9e74eba8`와 RDS/네트워크 스택 삭제가 완료됐다. 후속 조회에서 DB·두 스택은 이름으로 찾을 수 없고, 해당 앱 태그 보안 그룹, 수동 스냅샷, 관리형 비밀과 마이그레이션 로그 그룹 목록은 `[]`였다. 기존 `onedeploy-demo-app`은 `available`·삭제 보호 켜짐이다. 테스트의 전체 실행 코드는 장시간 폴링 오류로 1을 반환했지만, 브라우저 완료 재조회와 리소스 정리 검증은 별도로 통과했다. 이 실행의 실제 청구액은 확인하지 않았다.

```sh
python3 -m tests.smoke_aws_postgres_create_browser \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2
# 사전 점검만 수행한다. 새 임시 네트워크와 과금 가능한 RDS를 생성·정리할 때만 --apply 추가.
```

로컬 작업 기록은 `.onedeploy/browser-db-drills/<앱 ID>/`에만 둔다. 브라우저 프로필은 검증 후 삭제하고, 서버 생성 기록과 DB 정리 저널은 장애 분석을 위해 유지한다. 이 경로는 신규 DB 생성 UI를 검증하며 앱 ZIP 업로드와 실제 AI 판단은 별도 검증 범위다.

## 2026-10-02 스냅샷 이전 데이터 표식 복원 검증

`tests.smoke_aws_restore_marker_source`로 원본 `demo-app` DB에 고유 표식 행을 쓴 뒤 조회하고, 새 `onedeploy-demo-app-marker-20261002` 스냅샷이 `available`이 될 때까지 기다렸다. 이후 원본 표식 행을 삭제하고 임시 ECS Express 서비스·ECR 이미지를 정리했다. 로컬 `.onedeploy/restore-marker-source.json`에는 표식 ID와 작업 단계가 남으며 앱 접근 키는 성공 후 제거됐다. 이 파일은 Git 추적에서 제외한다.

기록된 경로로 `onedeploy-restore-demo-app-marker-20261002` 인스턴스를 격리 그룹에 복원하고 검사 그룹에서만 5432를 열었다. `postgres_restore_task_operations --marker-id <로컬 표식 ID>`의 실제 Fargate 작업이 마이그레이션 원장 1건의 이름·SHA-256과 복원 표식 행의 ID·값을 읽기 전용으로 대조했다. CloudWatch 로그는 `{"status":"passed","migration_count":1,"marker_checked":true}`였으며 태스크 종료 코드 0, 검사 이미지 태그 부재와 비활성 태스크 정의를 확인했다.

검사 연결을 닫고 복원 DB 삭제 완료 후 격리 그룹을 지웠다. `postgres_restore_drill_operations --finalize`는 SQL 성공과 임시 자원 부재를 확인해 로컬 기록을 `cleaned`로 마감했다. 새 표식 스냅샷은 `postgres_snapshot --inspect`로 원본·소유 태그·`available` 상태를 확인한 뒤 삭제했다. 마지막 수동 스냅샷 목록에는 기존 `onedeploy-demo-app-backup-20261002` 하나만 있고, 원본 RDS는 `available`·삭제 보호 켜짐이며 표식용 ECS 서비스는 `INACTIVE`다. 실제 청구 금액은 확인하지 않았다.

```sh
python3 -m tests.smoke_aws_restore_marker_source \
  --application demo-app --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --vpc-id <VPC_ID> --subnet-id <SUBNET_A> --subnet-id <SUBNET_B> \
  --service-security-group <APP_SERVICE_GROUP_ID> \
  --snapshot-id onedeploy-demo-app-marker-<UNIQUE_SUFFIX> \
  --state-file .onedeploy/restore-marker-source.json
# 위 명령은 읽기 전용 사전 계획이다. 실제 표식·스냅샷 생성 시에만 --apply 추가.
```

## 2026-10-02 복원 드릴 완료

서울 리전에서 `onedeploy-demo-app-backup-20261002`를 임시 DB `onedeploy-restore-demo-app-drill-20261002`로 복원했다. 로컬 작업 기록을 먼저 만들고 격리 그룹 `sg-0613f406d2cb9daf7`과 DB ARN을 저장했다. 생성 도중 `configuring-enhanced-monitoring`을 거쳐 `available`이 됐고, 소유 태그·비공개 연결·구성을 재확인했다.

임시 검사 그룹 `sg-0510cff6da1403d80`으로만 DB 5432를 열었다. 고정 비밀 버전과 ECR 이미지 digest를 사용한 Fargate 검사 태스크가 기존 마이그레이션 원장 1건의 이름·SHA-256을 `BEGIN READ ONLY`에서 대조했다. 태스크 종료 코드 0과 CloudWatch의 SQL 성공 메시지를 함께 확인했다. `--reconcile`에서 검사 이미지 태그가 없고 태스크 정의가 비활성 상태인 것도 재확인했다. 이 스냅샷에는 사전 데이터 표식이 없어 애플리케이션 행 데이터 비교는 수행하지 않았다.

검사 연결을 닫고 임시 DB 삭제 완료를 기다린 뒤 격리 그룹을 삭제했다. `--finalize`는 SQL 성공과 ECS·RDS·임시 그룹 정리 상태를 읽기 전용으로 대조하고 작업 기록을 `cleaned`로 마감했다. 원본 `onedeploy-demo-app`은 `available`·삭제 보호 켜짐이고 수동 스냅샷은 `available`·암호화 상태로 남아 있다. 임시 DB·그룹은 남지 않았다. 실제 청구 금액은 확인하지 않았다.

```sh
python3 -m onedeploy.postgres_restore_drill_operations \
  --finalize --state-dir .onedeploy/restore-drill \
  --verifier-state-dir .onedeploy/restore-verification \
  --application demo-app --snapshot-id onedeploy-demo-app-backup-20261002 \
  --target-id onedeploy-restore-demo-app-drill-20261002 \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --vpc-id <VPC_ID> \
  --service-security-group <APP_SERVICE_GROUP_ID>
```

## 2026-10-01 기존 DB 백업 상태 확인

서버 UI에서 앱 ID `demo-app`과 AWS ECS Express를 선택한 뒤 **기존 OneDeploy PostgreSQL RDS 사용** → **백업·보호 상태 확인**을 누른다. 인증 API `GET /api/applications/demo-app/postgres/backups`는 기존 DB 소유권 검사를 먼저 수행한다. 실제 서울 리전의 읽기 전용 조회에서 자동 백업 7일, 삭제 보호 켜짐, CloudFormation 스택 삭제 시 DB 보존, 수동 스냅샷 0개를 확인했다. UI 호출 자체의 실제 Chrome 검증과 스냅샷 생성·복원은 아직 수행하지 않았다.

수동 스냅샷 CLI의 실계정 계획은 아래 명령으로 통과했다. 기본 동작은 읽기 전용이다. `--apply`는 별도 생성 요청이며 백업 저장 비용이 발생할 수 있다. 생성한 경우에만 같은 입력에 `--inspect`를 붙여 상태·소유 태그를 확인한다. 현재 기록에서는 `--apply`를 실행하지 않았다.

2026-10-02 UI의 **기존 RDS 수동 스냅샷** 계획에 대응하는 인증 HTTP `POST /api/applications/demo-app/snapshots/plan`도 실제 서울 리전에서 `onedeploy-demo-app-before-migration` 대상으로 통과했다. 응답은 HTTP 200, 계정 `265233844540`, 기존 수동 스냅샷 0개였다. 이 HTTP 호출은 읽기 전용이었다.

같은 날 `--browser-read-only`로 실제 Chrome에서 기본 네트워크 자동 입력 → 기존 RDS 조회 → **백업·보호 상태 확인** → `browser-read-only` 이름의 수동 스냅샷 계획 확인을 통과했다. 화면에 `onedeploy-demo-app-browser-read-only`, 저장 비용 안내, 별도 생성 버튼이 나타났고 서버의 배포·스냅샷 생성 작업 기록은 비어 있었다. 생성 버튼은 누르지 않았다.

2026-10-02 `--snapshot-apply --snapshot-name backup-20261002`를 같은 계정·리전·앱으로 실행했다. Chrome에서 계획 확인 후 별도 생성 버튼을 눌렀고, 서버 작업 기록과 AWS 재확인이 `succeeded`/`available`로 끝났다. `onedeploy-demo-app-backup-20261002`는 암호화와 소유 태그를 확인해 **보존**했다. 후속 읽기 전용 조회에서 원본 DB `available`, 수동 스냅샷 1개를 확인했다. 이 스냅샷의 실제 청구액은 아직 확인하지 않았고, 복원 시험도 하지 않았다.

같은 스냅샷으로 아래 복원 드릴 계획을 읽기 전용으로 실행했다. 원본과 스냅샷의 VPC·엔진·20 GiB gp3 구성이 일치하고 `onedeploy-restore-demo-app-drill-20261002` ID가 비어 있음을 확인했다. 반환된 730시간 기준 기본 용량 견적은 20.87 USD로 실제 청구액이 아니다. 당시 복원 전용 보안 그룹과 정리 경로가 없으므로 복원 요청은 실행하지 않았다.

2026-10-02 같은 VPC에서 임시 대상 `onedeploy-restore-demo-app-netprobe-69d47d34`의 복원 전용 그룹 `sg-077ceef5e7b19bcf6`을 생성했다. 앱·대상 태그와 빈 인바운드·아웃바운드 규칙을 확인한 뒤 네트워크 인터페이스·보안 그룹 참조가 없을 때 그룹을 삭제하고 이름 조회로 삭제를 검증했다. 원본 RDS와 스냅샷은 그대로 보존했다. 실제 복원 DB는 만들지 않았다.

복원 인스턴스의 코드 경로는 다음 순서다. 아래 복원 DB 명령은 실제 리소스와 비용을 만들 수 있다. DB 생성 후 `--inspect`로 조회하고, 데이터 확인 후 `--delete`로 복원 DB 삭제를 요청한다. 삭제 완료를 AWS에서 확인한 다음 보안 그룹을 정리한다. 이 순서는 위 실계정 복원 드릴에서 통과했고 원본 스냅샷은 보존했다.

복원 시작은 `onedeploy.postgres_restore_drill_operations`를 사용한다. 기본 실행은 스냅샷·대상·격리 그룹의 읽기 전용 계획이고 `--apply`만 그룹과 과금 가능한 RDS 인스턴스를 만든다. 이 CLI는 `.onedeploy/restore-drill`에 로컬 기록을 먼저 저장한다. 응답이 불확실하면 같은 대상에 `--apply`를 반복하지 않고 `--reconcile`로 그룹·DB를 읽기 전용 재확인한다. 이 실행 경로는 위 실계정 복원에서 통과했다.

2026-10-02 서울 리전에서 이 새 CLI의 기본 읽기 전용 계획을 실행했다. 보존 스냅샷과 원본 DB의 계정·VPC·구성이 일치하고 대상 ID와 대상 그룹 이름이 비어 있음을 확인했다. 계획 조회는 그룹·DB를 만들지 않았으며 `--apply`는 실행하지 않았다.

```sh
python3 -m onedeploy.postgres_restore_drill_operations \
  --state-dir .onedeploy/restore-drill \
  --application demo-app --snapshot-id onedeploy-demo-app-backup-20261002 \
  --target-id onedeploy-restore-demo-app-drill-20261002 \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --vpc-id <VPC_ID> \
  --service-security-group <APP_SERVICE_GROUP_ID>
# 생성할 때만 --apply; 생성 결과 재확인은 --reconcile
```

데이터 검사 작업의 네트워크는 DB가 `available`이고 아래 `--inspect`가 통과한 뒤 `onedeploy.postgres_restore_probe_network`에 같은 앱·대상·계정·리전·VPC와 `--db-group-id <RESTORE_GROUP_ID>`를 지정해 준비한다. 기본 실행은 읽기 전용이고 `--apply`로 연결을 연다. 작업 종료 후 `--close <PROBE_GROUP_ID>`로 닫는다. 실제 복원 DB의 읽기 전용 SQL 검사에서 이 연결·해제를 통과했다.

SQL 검사 이미지 문맥은 `stage_restore_verifier_context`로 준비한다. 기존 마이그레이션 manifest만 포함해 복원 DB 원장의 이름·SHA-256과 선택적 검사 행을 `BEGIN READ ONLY`에서 대조한다. Python·Node 단위 테스트는 통과했다. 이 단계만으로 복원 데이터 검증이 완료됐다고 기록하지 않는다.

이후 `onedeploy.postgres_restore_task`의 읽기 전용 사전 계획과 `RestoreVerifierRunner` 실행 코드가 추가됐다. 이미지 digest·고정 비밀 버전·검사 그룹을 task definition에 넣고, 소유 ECS 작업 종료 코드와 CloudWatch SQL 성공 로그를 함께 확인한다. `onedeploy.postgres_restore_task_operations`는 대상별 로컬 기록을 AWS 변경 전에 만들고 실행 단계를 저장한다. 같은 대상의 중복 시작을 차단하며 중단 후 `--reconcile`은 STS·ECR·ECS·CloudWatch를 읽기 전용으로 조회한다. 작업 ARN 없이 시작 요청이 불확실하면 자동 재실행하지 않는다. 실제 복원 DB의 ECS 검사와 정리 재확인을 통과했다.

복원 DB와 검사용 네트워크를 준비한 후에만 다음과 같이 실행한다. `--apply`는 이미지 업로드와 과금 가능한 ECS 작업을 시작한다. 상태 디렉터리는 `.onedeploy/` 아래에 두어 Git 추적에서 제외한다. 같은 대상의 재시도는 기록을 지워 강행하지 않고 먼저 `--reconcile`로 조사한다.

```sh
python3 -m onedeploy.postgres_restore_task_operations \
  --apply --state-dir .onedeploy/restore-verification \
  --application demo-app --snapshot-id onedeploy-demo-app-backup-20261002 \
  --target-id onedeploy-restore-demo-app-drill-20261002 \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --vpc-id <VPC_ID> --db-group-id <RESTORE_DB_GROUP_ID> \
  --probe-group-id <RESTORE_PROBE_GROUP_ID> \
  --service-security-group <APP_SERVICE_GROUP_ID> --project <APP_DIRECTORY>
python3 -m onedeploy.postgres_restore_task_operations \
  --reconcile --state-dir .onedeploy/restore-verification \
  --target-id onedeploy-restore-demo-app-drill-20261002 \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2
```

2026-10-02 `onedeploy.postgres_restore_credentials`를 서울 리전의 원본 RDS·보존 스냅샷·관리형 비밀 메타데이터에 읽기 전용으로 실행했다. 현재 `AWSCURRENT` 버전의 생성 시각은 2026-09-30 15:47:53 UTC이고 스냅샷 생성 시각은 2026-10-01 15:10:08 UTC였다. 계획은 해당 버전 ID를 ECS 비밀 참조에 고정했으며 비밀번호 값은 조회하지 않았다. 이후 실제 복원 DB의 ECS 검사에서 인증과 SQL 조회가 성공했다.

```sh
python3 -m onedeploy.postgres_restore_credentials \
  --application demo-app --snapshot-id onedeploy-demo-app-backup-20261002 \
  --target-id onedeploy-restore-demo-app-drill-20261002 \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --service-security-group <EXISTING_APP_SERVICE_GROUP_ID>
```

```sh
python3 -m onedeploy.postgres_restore_instance \
  --application demo-app --snapshot-id onedeploy-demo-app-backup-20261002 \
  --target-id onedeploy-restore-demo-app-drill-20261002 \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --vpc-id <VPC_ID> --group-id <RESTORE_GROUP_ID> \
  --service-security-group <EXISTING_APP_SERVICE_GROUP_ID>
# 실제 복원 요청 시만 --apply, 조회는 --inspect, 복원 DB 삭제 요청은 --delete
```

기존 앱 네트워크 스택이 없으면 원본 RDS의 소유권 점검에 기존 앱 서비스 보안 그룹 ID가 필요하다. 2026-10-02 해당 ID를 지정한 복원 계획 로직을 서울 리전에서 읽기 전용으로 통과했다. 위 인스턴스 CLI의 기본 사전 점검은 복원 전용 그룹도 실제로 존재하고 소유권·규칙 검증을 통과해야 완료된다. 복원 전용 그룹은 별도로 생성해야 한다.

2026-10-02 `onedeploy-restore-demo-app-probe-cf3ac00a` 임시 대상으로 복원 DB 그룹과 검사 작업 그룹을 생성했다. 검사 그룹 `sg-08cfa968d0ea4785c`에서 DB 그룹의 TCP 5432와 HTTPS 443으로만 나가고, DB 그룹은 그 검사 그룹의 TCP 5432만 받도록 검증했다. 검사 그룹 연결을 닫고 두 그룹을 삭제했다. 실제 DB나 ECS 작업은 생성하지 않았다.

```sh
PYTHONPATH=. python3 tests/smoke_aws_restore_probe_network.py \
  --application demo-app --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --vpc-id <VPC_ID>
# 임시 그룹 두 개의 실제 연결·해제·정리를 실행할 때만 --apply 추가
```

```sh
PYTHONPATH=. python3 tests/smoke_aws_restore_network.py \
  --application demo-app --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --vpc-id <VPC_ID>
# 새 임시 그룹 생성·검증·정리를 실행할 때만 --apply 추가
```

```sh
python3 -m onedeploy.postgres_restore --application demo-app \
  --snapshot-id onedeploy-demo-app-backup-20261002 \
  --target-id onedeploy-restore-demo-app-drill-20261002 \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --service-security-group <SERVICE_GROUP_ID>
```

```sh
PYTHONPATH=. python3 tests/smoke_aws_postgres_browser.py \
  --application demo-app --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --service-security-group <SERVICE_GROUP_ID> \
  --snapshot-apply --snapshot-name <새 스냅샷 이름>
```

```sh
python3 -m onedeploy.postgres_snapshot --application demo-app \
  --snapshot-id onedeploy-demo-app-before-migration \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --service-security-group <SERVICE_GROUP_ID>
```

## 2026-10-01 임시 앱 네트워크 스택 검증

`PYTHONPATH=. python3 tests/smoke_aws_network.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2`는 기본 VPC와 새 `netprobe-*` 앱 ID를 읽기 전용으로 사전 점검한다. `--apply`를 추가하면 앱 전용 보안 그룹 스택을 생성하고 서버의 앱별 그룹 선택을 검증한 뒤, 그룹 사용·참조 여부를 확인해 그 임시 스택을 정리한다. 생성 결과가 불확실하거나 다른 리소스에서 그룹을 사용하면 자동 정리를 멈추고 스택 이름을 출력한다.

서울 리전에서 `netprobe-3300267c`의 `--apply` 검증을 통과했다. 스택 삭제 완료 후 앱 태그의 보안 그룹이 남지 않았음을 읽기 전용으로 재조회했다. RDS·ECS는 생성하지 않았다. 이 smoke는 CLI 생성기와 서버의 앱별 선택 함수를 검증한다.

별도 `PYTHONPATH=. python3 tests/smoke_aws_network_browser.py --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --apply`는 실제 Chrome에서 기본 VPC 자동 입력, 읽기 전용 네트워크 계획, 생성 버튼·상태 표시를 검증한다. 서울 리전의 임시 `netprobe-9b93b64b` 앱에서 통과했고 서버 작업 기록의 스택 ARN·그룹 ID를 AWS와 대조했다. 임시 스택 삭제 완료 후 같은 앱 태그의 보안 그룹이 없음을 재조회했다. 이 브라우저 smoke에도 새 RDS·ECS 생성은 포함되지 않는다.

이 절차는 서울 리전의 기존 기본 VPC에서 `demo-app`을 검증 대상으로 삼은 기록이다.
실행 전에 STS 계정·리전·대상 ID를 다시 확인한다. 2026-10-01 읽기 전용 조회에서는
두 가용 영역과 PostgreSQL 18.3 `db.t4g.micro`/암호화된 `gp3` 20 GiB가 사용 가능했다.
RDS 기본 용량의 730시간 기준 공개 가격은 20.87 USD였다. 백업 초과분·데이터 전송·
Secrets Manager·로그·ECS/Fargate/ALB·세금은 포함되지 않아 총 청구액의 상한이 아니다.

## 생성 순서

1. `aws sts get-caller-identity`로 배포 계정을 확인한다. `onedeploy-demo-app-service`라는
   전용 보안 그룹을 대상 기본 VPC에 만들고 인바운드는 비운다. 그룹에는
   `onedeploy-managed=true`, `onedeploy-app=demo-app` 태그를 지정한다. 기존 다른
   ECS 서비스가 사용 중인 보안 그룹은 재사용하지 않는다.
2. `python3 -m onedeploy.postgres`에 앱 ID, 계정, 리전, 기본 VPC, 서로 다른 두
   가용 영역의 서브넷과 새 서비스 보안 그룹 ID를 넣어 **`--apply` 없이** 사전 점검한다.
   네트워크·소유 태그·주문 가능 구성·가격이 확인돼야 한다.
3. 같은 입력에 명시적 `--apply`를 붙여 `onedeploy-db-demo-app` 스택을 생성한다.
   템플릿은 비공개·암호화·삭제 보호된 RDS, 7일 백업, 관리형 비밀,
   앱 전용 DB 보안 그룹·서브넷 그룹·ECS 실행 역할·14일 로그 그룹을 만든다.
   스택 종료 보호와 RDS `Retain`이 적용돼 앱 실패/종료로 DB가 삭제되지 않는다.
4. 같은 입력으로 `python3 -m onedeploy.postgres --inspect`를 실행해 계정,
   스택·서비스 보안 그룹 소유권, 비공개 연결, 암호화·삭제 보호, 비밀·역할을
   읽기 전용으로 대조한다. 비밀 원문은 읽지 않는다.
5. `PYTHONPATH=. python3 tests/smoke_aws_postgres.py`를 먼저 `--apply` 없이
   실행하고, 이어서 `--apply`로 일회성 검증을 실행한다. SQL 마이그레이션의
   중복 방지, 서비스 v1→v2 URL 유지, DB 쓰기·읽기·재시작 후 보존을 확인한다.

smoke가 성공하면 임시 ECS 서비스와 ECR 이미지 태그를 정리한다. 실패·중단 시에는
기록된 서비스/태스크/이미지 ARN의 실제 상태를 읽기 전용으로 확인한 뒤 소유권이
증명된 리소스만 정리한다. RDS·관리형 비밀·앱 전용 서비스 보안 그룹은 자동으로
삭제하지 않는다. DB 폐기는 별도 스냅샷·보호 해제·소유권 검증 절차가 필요하다.
이 실계정 검증을 마치기 전에는 일반 UI의 DB 앱 차단을 유지한다.

## 2026-10-01 실행 결과

- `demo-app` 전용 서비스 보안 그룹과 `onedeploy-db-demo-app` 스택을 생성했다.
  PostgreSQL 18.3 RDS 인스턴스는 `available`이며 비공개·암호화·삭제 보호를
  읽기 전용 점검으로 확인했다. RDS·비밀·보안 그룹은 현재 보존 중이고 비용이 발생한다.
- 첫 마이그레이션 태스크는 Node가 RDS CA를 신뢰하지 못해 종료 코드 1로 실패했다.
  [AWS 공식 RDS CA 번들](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html)을
  체크섬으로 고정해 이미지에 포함하고 TLS 서버 인증을 유지한 채 재실행했다.
  실패한 태스크의 정의와 ECR 이미지 태그는 소유권·종료 상태를 확인하고 정리했다.
- 재실행한 smoke에서 v1의 SQL 마이그레이션과 DB 쓰기/읽기, v2의 중복 방지
  마이그레이션, 동일 서비스 URL과 기존 행 읽기, 검증 행 삭제가 통과했다.
  임시 ECS 서비스는 `INACTIVE`가 됐고 두 앱 이미지 태그는 삭제됐다.
- 검증용 앱의 Dockerfile만 CA를 포함하는 방식을 걷어내고, 일반 이미지 빌더가
  DB 앱 이미지에 고정된 CA를 자동 주입하도록 수정했다. 이 최종 코드 경로로
  같은 v1→v2 smoke를 다시 실행해 통과했고 임시 서비스·태그 정리도 확인했다.
- 위 v1→v2 smoke는 내부 AWS 어댑터 경로다. UI의 DB 자동 생성·선택·배포는
  별도 구현과 검증이 필요하므로 일반 UI의 차단을 유지한다.
- 별도 `tests/smoke_aws_postgres_api.py`로 ZIP 업로드, 기존 RDS 소유권 확인,
  환경값 요청·재개, ECS 배포, HTTP 데이터 쓰기/읽기, `/health`, 종료 API까지
  실계정에서 통과했다. AI 도구 호출은 고정 테스트 응답이었다. 임시 ECS 서비스·
  앱 이미지 태그는 정리했으며 RDS와 비밀은 유지한다.

## 2026-10-01 실제 Chrome UI 경로

`tests/smoke_aws_postgres_browser.py`의 기본 모드는 기존 RDS를 읽기 전용으로 확인한다. `--apply`에서만 Chrome의 배포 UI가 기존 DB 조회·ZIP 업로드·필수 값 입력·재개를 실행한다. AI 도구 호출은 고정 응답이다. 서울 리전의 `demo-app` RDS에서 이 경로를 실행해 ECS 배포, HTTP 데이터 쓰기·읽기·삭제, 임시 ECS 서비스·이미지 정리를 통과했다. RDS·비밀·앱 전용 보안 그룹은 보존했다.

`--browser-read-only`는 실제 Chrome에서 **기본 VPC·서브넷 불러오기**와 **기존 RDS 조회**를 누르고 입력란의 VPC 일치를 확인한다. 2026-10-01 서울 리전에서 통과했으며 배포 작업·새 AWS 리소스를 만들지 않았다.

```sh
PYTHONPATH=. python3 tests/smoke_aws_postgres_browser.py \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 \
  --service-security-group <APP_OWNED_SERVICE_GROUP_ID>
# 실제 Chrome에서 조회만 확인하려면 --browser-read-only 추가
# 실제 ECS 서비스와 이미지 빌드를 실행할 때만 --apply 추가
```
## 사용자 DB 폐기 경로의 읽기 전용 검증

2026-10-02 `python3 -m onedeploy.postgres_retirement`의 기본 계획을 보존 중인
`demo-app`에 실행했다. 계정 `265233844540`, 서울 리전, DB
`onedeploy-demo-app`과 소유 RDS 스택을 재확인했다. DB 삭제 보호는 켜져 있고,
ECS 서비스·실행 중인 작업에서 해당 DB 비밀을 사용하는 대상은 0건이었다.
서버 API가 사용하는 `PostgresRetirementOperations.plan()`도 같은 보존 DB에서
읽기 전용으로 통과했으며 `final_snapshot_required: true`를 반환했다.
이 호출은 DB·스냅샷·스택을 변경하지 않았다. `--apply`는 실행하지 않았으며,
최종 스냅샷 생성과 실제 폐기, 작업 중단 후 AWS 상태 수동 대조는 임시 앱으로
별도 검증해야 한다. 원본 DB와 기존 수동 스냅샷은 보존 중이다.

같은 날 별도 임시 앱 `retire-7c3a9d21`의 네트워크 스택·암호화된 PostgreSQL 18.3
`db.t4g.micro`/gp3 20 GiB RDS 스택을 생성했다. 폐기 CLI의 읽기 전용 계획에서
앱 소유권·삭제 보호와 DB 비밀을 사용하는 ECS 사용자 0명을 확인했다. 새 로컬
기록 `.onedeploy/retirement-live-drill/retire-7c3a9d21.json`을 만든 뒤 정확한 DB ID로
`--apply`를 실행했다. 최종 스냅샷 `onedeploy-retire-7c3a9d21-final-b501205769f3`가
소유 태그를 가진 암호화된 `available` 상태인 것을 확인하고 DB 보호 해제·삭제,
RDS 스택 `DELETE_COMPLETE`, 삭제 후 스냅샷 `available` 재확인을 통과했다.
이 임시 최종 스냅샷을 별도로 삭제하고 미사용 앱 네트워크 스택도 삭제했다.
마지막 AWS 조회에서 임시 DB·수동 스냅샷·앱 태그 보안 그룹·Secrets Manager 비밀·
마이그레이션 로그 그룹은 목록에 없고, RDS·네트워크 스택은 모두
`DELETE_COMPLETE`였다. 기존 `onedeploy-demo-app`은
`available`·삭제 보호 켜짐이며 `onedeploy-demo-app-backup-20261002`는 암호화된
`available`로 남았다. 실제 Chrome에서는 보존 `demo-app`의 읽기 전용 폐기 계획에
계정·DB ID·삭제 보호와 별도 실행 버튼이 표시됐고, 잘못된 DB ID를 입력하면
실행하지 않음을 확인했다. 서버의 폐기 작업 기록도 비어 있었다. 유효 DB ID로
브라우저에서 실제 폐기하는 흐름과 강제 중단 시험은 아직 하지 않았다.
이 임시 RDS와 스냅샷의 실제 청구액은 확인하지 않았다.
