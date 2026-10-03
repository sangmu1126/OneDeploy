# 보존된 AWS 자원의 Terraform 기록

서울 리전의 계정 `265233844540`에서 2026-10-03 확인한 자원 네 개를 정의한다. 공유 `onedeploy-core` 스택은 ECR 저장소와 ECS 역할 두 개, `onedeploy-db-demo-app` 스택은 보호된 PostgreSQL RDS와 관리형 비밀·DB 보안 그룹·로그 그룹을 소유한다. 앱 서비스 보안 그룹은 스택 밖에 있으며, 무상태 데모 ECS Express 서비스는 자체 로드 밸런서·보안 그룹을 소유한다. Terraform에는 스택과 서비스만 등록해 중복 소유를 피한다.

기존 자원의 import 식별자는 `main.tf`에 있다. 2026-10-03에 네 자원을 로컬 `terraform.tfstate`로 import했고 `terraform validate`가 통과했다. `terraform plan`에는 AWS provider 6.67.0이 import한 ECS Express 서비스의 기본 `wait_for_steady_state=false`를 추가하려는 **제자리 갱신 1건**이 남는다. 이 계획은 적용하지 않았다. 데이터베이스 스택의 매개변수도 provider가 빈 맵으로 읽어 잘못된 갱신을 제안하므로 해당 필드만 변경 무시한다. **계획 확인 없이 `terraform apply`나 `terraform destroy`를 실행하지 않는다.** 이 디렉터리의 로컬 `terraform.tfstate`와 `.terraform/`은 Git에서 제외한다. TF state는 실행 자원 식별자와 메타데이터를 담으므로 별도로 안전하게 보관한다. 이 파일 자체는 스냅샷이나 컨테이너 이미지를 백업하지 않는다.

```sh
cd terraform/aws-live
terraform init
terraform validate
terraform plan
```

현재 OneDeploy 실행기도 같은 CloudFormation 스택과 ECS 서비스를 조작할 수 있다. 두 도구를 동시에 변경 실행하지 않는다. Terraform의 `prevent_destroy`는 실수로 스택·DB·서비스를 삭제하는 계획을 막는다. RDS는 별도 수동 스냅샷 `onedeploy-demo-app-backup-20261002`가 보존돼 있다. Terraform 구성의 ECS 서비스는 기존 배포를 기록하고 재생성할 수 있게 하되, OneDeploy가 실제 업데이트를 담당하므로 변경은 무시한다.

2026-10-03 일시 중지에서는 ECS Express 최소 태스크를 0으로 바꾸고 기본 ECS 서비스의 목표 태스크도 0으로 설정했다. 서비스·로드 밸런서는 남아 있어 비용이 계속 발생할 수 있다. RDS에는 `stop-db-instance`를 요청했다. RDS의 인스턴스 메타데이터와 데이터는 유지되지만 스토리지·백업 비용은 계속 발생하고, AWS가 **7일 뒤 자동 재시작**할 수 있다. 종료 확인 결과는 [일시 중지 기록](../../docs/aws-pause-2026-10-03.md)을 따른다.

내일 다시 시작할 때는 DB를 먼저 켜고 `available`을 확인한 뒤 ECS Express 최소 태스크와 기본 ECS 목표 태스크를 각각 1로 복원한다. `terraform apply`로 이 중지를 해제하지 않는다.

```sh
aws rds start-db-instance --db-instance-identifier onedeploy-demo-app --region ap-northeast-2
aws rds wait db-instance-available --db-instance-identifier onedeploy-demo-app --region ap-northeast-2
aws ecs update-express-gateway-service \
  --service-arn arn:aws:ecs:ap-northeast-2:265233844540:service/default/onedeploy-8265dc56c6d74adf-a1 \
  --scaling-target minTaskCount=1,maxTaskCount=1,autoScalingMetric=AVERAGE_CPU,autoScalingTargetValue=60 \
  --region ap-northeast-2
aws ecs update-service --cluster default --service onedeploy-8265dc56c6d74adf-a1 \
  --desired-count 1 --region ap-northeast-2
```
