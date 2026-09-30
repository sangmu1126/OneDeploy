# AWS 영속 데이터 배포 경로 설계

현재 OneDeploy UI는 PostgreSQL·MySQL·MongoDB 등 데이터베이스 의존 앱을 배포 전에 차단한다. 아래는 AWS에서 **PostgreSQL 한 경로**를 실제로 지원하기 위한 설계와 부분 구현이다. DB 생성·접속 연결 코드는 있지만 서버의 원클릭 배포와 데이터 이전은 완성되지 않았다.

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

현재 구현은 기본 VPC에서 인바운드 규칙이 없는 추가 ECS 서비스 보안 그룹 하나를 선택하고 실제 적용을
검증한다. 별도의 `onedeploy.postgres` 명령은 지정한 기본 VPC의 두 가용 영역을 확인하고,
PostgreSQL 인스턴스·DB 보안 그룹·서브넷 그룹을 **명시적 `--apply`일 때만** 새 스택으로 생성한다.
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
일반 업로드 UI는 아직 이 경로를 호출하지 않으며, 스키마 마이그레이션·DB 읽기/쓰기 검증 전에는
DB 앱 배포를 계속 차단한다. 스택 삭제나 교체에도 DB를 보존하고 삭제 보호를
켜고 스택 종료 보호도 적용하므로, 앱 배포 실패·서비스 종료가 데이터를 지우지 않는다. 이 설정은 계속 비용이 발생할 수 있으며,
DB 폐기는 별도 스냅샷·보호 해제·소유권 검증 절차가 필요하다.

내부 배포 도구에는 명시적 `postgres_request` 입력 경로가 있다. 이 경로는 소스에서
PostgreSQL 엔진만 확인된 AWS 앱에만 적용하고, `PGHOST`·`PGPASSWORD` 등 관리형 변수는
사용자에게 다시 요청하지 않고 검증된 DB 바인딩을 AWS 어댑터에 전달한다.
MySQL·MongoDB·혼합/불명 엔진과 `DATABASE_URL` 접속 방식은 이 경로에서 차단한다.
서버 업로드 API에는 아직 이 옵션을 노출하지 않는다.

마이그레이션 실행기의 로컬 구성도 준비했다. 앱의 `migrations/0001_name.sql` 형식 SQL 파일을
최대 32개·파일당 64 KiB로 검증하고, 파일명과 SHA-256을 고정한 별도 Docker 빌드 문맥을 만든다.
실행기는 PostgreSQL의 트랜잭션별 advisory lock과 `onedeploy_schema_migrations` 이력으로
같은 파일의 중복 적용을 건너뛰고, 적용한 파일의 체크섬이 바뀌면 실패한다.
파일 안의 트랜잭션 제어문은 허용하지 않는다. **이 실행기를 ECS 일회성 태스크로 호출하는 경로는
아직 없으며**, 원클릭 DB 앱 배포의 마이그레이션 완료를 주장하지 않는다.

```sh
python3 -m onedeploy.postgres --application demo-app --account <AWS_ACCOUNT_ID> \
  --region ap-northeast-2 --vpc-id <DEFAULT_VPC_ID> \
  --subnet-id <SUBNET_A_ID> --subnet-id <SUBNET_B_ID> \
  --service-security-group <NO_INGRESS_GROUP_ID>
# 실제 생성할 때만 마지막에 --apply 추가
# 기존 DB의 소유권·암호화·비공개 설정·서브넷·보안 그룹을 읽기 전용으로 재확인하려면 --inspect 추가
```

이 명령은 **DB 리소스만 준비**한다. 어댑터의 옵션 경로는 앱 접속 설정을 준비하지만,
마이그레이션, DB 읽기·쓰기 검증, 앱 업데이트·종료와 DB 수명주기 연결은 아직 없다.
`--apply`를 실행해도 OneDeploy 제품 UI에서
DB 의존 앱 차단은 해제되지 않는다. `--inspect`는 지정한 AWS 계정과 스택 소유권을 확인하고,
RDS의 암호화·삭제 보호·비공개 엔드포인트·서브넷, DB 보안 그룹의 5432 인바운드 범위를 대조한다.
실제 AWS 계정에서 이 생성·점검 경로는 아직 검증하지 않았다.

기존 DB를 이용한 라이브 데이터 경로 smoke는 별도로 준비했다. 기본 실행은 `--inspect`와 같은
읽기 전용 검증만 한다. `--apply`를 지정하면 인증 헤더가 필요한 임시 Node.js 앱을 ECS에 배포해
PostgreSQL에 임의 ID를 쓰고 읽은 뒤, 같은 서비스의 새 이미지 리비전에서도 그 값을 읽는다.
성공 시 검증 행을 삭제하고 임시 ECS 서비스와 이미지 태그를 정리한다. RDS·비밀과 smoke용 테이블은 보존된다.
실제 AWS 계정에서 이 smoke는 아직 실행하지 않았다.

```sh
PYTHONPATH=. python3 tests/smoke_aws_postgres.py --application demo-app \
  --account <AWS_ACCOUNT_ID> --region ap-northeast-2 --vpc-id <DEFAULT_VPC_ID> \
  --subnet-id <SUBNET_A_ID> --subnet-id <SUBNET_B_ID> \
  --service-security-group <NO_INGRESS_GROUP_ID>
# ECS와 ECR을 실제 사용하고 비용을 발생시킬 때만 --apply 추가
```
