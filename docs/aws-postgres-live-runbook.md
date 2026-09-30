# AWS PostgreSQL 실계정 검증 절차

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
