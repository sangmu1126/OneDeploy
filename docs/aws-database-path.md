# AWS 영속 데이터 배포 경로 설계

현재 OneDeploy는 PostgreSQL·MySQL·MongoDB 등 데이터베이스 의존 앱을 배포 전에 차단한다. 아래는 AWS에서 **PostgreSQL 한 경로**를 실제로 지원하기 위한 설계이며, 아직 DB 생성·이전 기능이 구현됐다는 뜻은 아니다.

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
