# AWS PostgreSQL 실계정 검증 절차

## 2026-10-01 기존 DB 백업 상태 확인

서버 UI에서 앱 ID `demo-app`과 AWS ECS Express를 선택한 뒤 **기존 OneDeploy PostgreSQL RDS 사용** → **백업·보호 상태 확인**을 누른다. 인증 API `GET /api/applications/demo-app/postgres/backups`는 기존 DB 소유권 검사를 먼저 수행한다. 실제 서울 리전의 읽기 전용 조회에서 자동 백업 7일, 삭제 보호 켜짐, CloudFormation 스택 삭제 시 DB 보존, 수동 스냅샷 0개를 확인했다. UI 호출 자체의 실제 Chrome 검증과 스냅샷 생성·복원은 아직 수행하지 않았다.

수동 스냅샷 CLI의 실계정 계획은 아래 명령으로 통과했다. 기본 동작은 읽기 전용이다. `--apply`는 별도 생성 요청이며 백업 저장 비용이 발생할 수 있다. 생성한 경우에만 같은 입력에 `--inspect`를 붙여 상태·소유 태그를 확인한다. 현재 기록에서는 `--apply`를 실행하지 않았다.

2026-10-02 UI의 **기존 RDS 수동 스냅샷** 계획에 대응하는 인증 HTTP `POST /api/applications/demo-app/snapshots/plan`도 실제 서울 리전에서 `onedeploy-demo-app-before-migration` 대상으로 통과했다. 응답은 HTTP 200, 계정 `265233844540`, 기존 수동 스냅샷 0개였다. 이 HTTP 호출은 읽기 전용이었다.

같은 날 `--browser-read-only`로 실제 Chrome에서 기본 네트워크 자동 입력 → 기존 RDS 조회 → **백업·보호 상태 확인** → `browser-read-only` 이름의 수동 스냅샷 계획 확인을 통과했다. 화면에 `onedeploy-demo-app-browser-read-only`, 저장 비용 안내, 별도 생성 버튼이 나타났고 서버의 배포·스냅샷 생성 작업 기록은 비어 있었다. 생성 버튼은 누르지 않았다.

2026-10-02 `--snapshot-apply --snapshot-name backup-20261002`를 같은 계정·리전·앱으로 실행했다. Chrome에서 계획 확인 후 별도 생성 버튼을 눌렀고, 서버 작업 기록과 AWS 재확인이 `succeeded`/`available`로 끝났다. `onedeploy-demo-app-backup-20261002`는 암호화와 소유 태그를 확인해 **보존**했다. 후속 읽기 전용 조회에서 원본 DB `available`, 수동 스냅샷 1개를 확인했다. 이 스냅샷의 실제 청구액은 아직 확인하지 않았고, 복원 시험도 하지 않았다.

같은 스냅샷으로 아래 복원 드릴 계획을 읽기 전용으로 실행했다. 원본과 스냅샷의 VPC·엔진·20 GiB gp3 구성이 일치하고 `onedeploy-restore-demo-app-drill-20261002` ID가 비어 있음을 확인했다. 반환된 730시간 기준 기본 용량 견적은 20.87 USD로 실제 청구액이 아니다. 당시 복원 전용 보안 그룹과 정리 경로가 없으므로 복원 요청은 실행하지 않았다.

2026-10-02 같은 VPC에서 임시 대상 `onedeploy-restore-demo-app-netprobe-69d47d34`의 복원 전용 그룹 `sg-077ceef5e7b19bcf6`을 생성했다. 앱·대상 태그와 빈 인바운드·아웃바운드 규칙을 확인한 뒤 네트워크 인터페이스·보안 그룹 참조가 없을 때 그룹을 삭제하고 이름 조회로 삭제를 검증했다. 원본 RDS와 스냅샷은 그대로 보존했다. 실제 복원 DB는 만들지 않았다.

복원 인스턴스의 코드 경로는 다음 순서다. 아래 복원 DB 명령은 실제 리소스와 비용을 만들 수 있으며 이 경로의 실계정 검증은 아직 하지 않았다. DB 생성 후 `--inspect`로 조회하고, 데이터 확인 후 `--delete`로 복원 DB 삭제를 요청한다. 삭제 완료를 AWS에서 확인한 다음 보안 그룹을 정리한다. 원본 스냅샷은 보존한다.

데이터 검사 작업의 네트워크는 DB가 `available`이고 아래 `--inspect`가 통과한 뒤 `onedeploy.postgres_restore_probe_network`에 같은 앱·대상·계정·리전·VPC와 `--db-group-id <RESTORE_GROUP_ID>`를 지정해 준비한다. 기본 실행은 읽기 전용이고 `--apply`로 연결을 연다. 작업 종료 후 `--close <PROBE_GROUP_ID>`로 닫고 DB `--inspect`를 다시 실행한다. 이 연결 절차만 검증됐으며, 실제 읽기 전용 SQL 작업은 아직 없다.

SQL 검사 이미지 문맥은 `stage_restore_verifier_context`로 준비한다. 기존 마이그레이션 manifest만 포함해 복원 DB 원장의 이름·SHA-256과 선택적 검사 행을 `BEGIN READ ONLY`에서 대조한다. Python·Node 단위 테스트는 통과했다. 이 단계만으로 복원 데이터 검증이 완료됐다고 기록하지 않는다.

이후 `onedeploy.postgres_restore_task`의 읽기 전용 사전 계획과 `RestoreVerifierRunner` 실행 코드가 추가됐다. 이미지 digest·고정 비밀 버전·검사 그룹을 task definition에 넣고, 소유 ECS 작업 종료 코드와 CloudWatch SQL 성공 로그를 함께 확인한다. 현재는 단위 테스트만 통과했으며 실제 복원 DB와 ECS 작업은 만들지 않았다. 중단 후 재확인 기록이 연결되기 전에는 이 실행기를 실계정에서 적용하지 않는다.

2026-10-02 `onedeploy.postgres_restore_credentials`를 서울 리전의 원본 RDS·보존 스냅샷·관리형 비밀 메타데이터에 읽기 전용으로 실행했다. 현재 `AWSCURRENT` 버전의 생성 시각은 2026-09-30 15:47:53 UTC이고 스냅샷 생성 시각은 2026-10-01 15:10:08 UTC였다. 계획은 해당 버전 ID를 ECS 비밀 참조에 고정했으며 비밀번호 값은 조회하지 않았다. 실제 복원 DB 연결 성공은 아직 확인하지 않았다.

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
