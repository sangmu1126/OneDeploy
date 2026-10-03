# OneDeploy 개발 기록 안내

이 문서는 저장소에서 확인할 수 있는 개발 기록의 시작점이다. 기능별 변경은 Git 커밋에,
설계 근거와 실계정 검증 결과는 아래 문서에 남겼다.

| 문서 | 내용 |
| --- | --- |
| [현재 상태](status.md) | 구현·검증 범위와 남은 작업 |
| [의사결정 기록](decision-log.md) | 기능별 판단 근거, 안전 경계, 실패·복구 경험 |
| [AWS PostgreSQL 실계정 검증 기록](aws-postgres-live-runbook.md) | 실제 생성·배포·복원·폐기 순서와 확인 결과 |
| [AWS 데이터 경로](aws-database-path.md) | RDS·ECS·스냅샷의 동작과 운영 절차 |
| [실패 생성 정리](aws-postgres-failed-create-recovery.md) | 롤백 스택 정리 조건, 작업 기록, 재생성 경계 |
| [요구사항](requirements.md) · [설계](design.md) | 해커톤 원문 해석과 구현 구조 |

2026-10-03까지 확인된 주요 흐름은 앱 업로드와 코드 준비, AWS ECS Express 배포,
기존·신규 PostgreSQL 연결, SQL 마이그레이션, HTTP 데이터 쓰기·읽기, RDS 스냅샷
복원·검사, 임시 DB의 최종 스냅샷 선행 폐기다. 각 흐름의 검증 범위와 남은 제한은
[현재 상태](status.md)에 구분해 적었다. 실제 AI 모델을 사용한 끝단 검증과
브라우저의 유효 DB ID를 통한 폐기 실행은 통과했다. 실패 생성 정리 제품 경로는
로컬 모의 응답과 실제 Chrome에서 검증했으며 실계정 드릴은 아직 실행하지 않았다.
기존 RDS를 명시한 자동 대상 배포는 Chrome에서 VPC·서브넷 수동 입력 없이 ZIP을
업로드해 앱 소유 DB 바인딩과 AWS 정책 계획이 기록되는 것까지 확인했다. 이 로컬
드릴은 실제 에이전트 배포 실행을 막았으므로 공개 HTTP 성공 증거는 아니다.
보존된 AWS 자원의 Terraform 구성은 원격 상태를 새로 읽는 plan으로 다시 확인했다.
ECS Express provider의 제자리 갱신 1건이 남아 있어 apply하지 않았고, 이 변경과
예상 밖의 변경을 구분하되 어느 쪽도 자동 적용하지 않는 [plan 점검기](../terraform/aws-live/README.md)를 추가했다.
로컬에서만 실행하던 Python·Node 회귀 테스트는 GitHub Actions의 `main` 푸시와
PR에서도 실행하도록 연결했다. 이 자동 검증에는 AWS 자격 증명이나 실제 배포가 없다.

로컬 `.onedeploy/`에는 임시 작업의 원본 JSON·로그·ZIP과 식별자가 들어 있다.
이 폴더는 Git에서 제외하며 공유용 개발 기록으로 사용하지 않는다. 공유 가능한
결과와 판단 근거는 위 문서에 정리한다. 과거 변경 순서는 `git log --oneline`
으로 볼 수 있고, 코드 변경의 검증은 `python3 -m unittest discover -s tests`와
관련 UI 테스트에서 확인할 수 있다.
