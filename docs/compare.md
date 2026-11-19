# 다른 방식과의 비교

Solo-API는 API의 모양(URL, 메서드, 스키마)을 정하는 규격이 아니다. **누가 무엇을 만들고, 어디에 경계를 두는지**를 정한다.
그래서 아래 방식들과 경쟁하기보다 옆에 놓인다.

| 방식 | 계약을 만드는 쪽 | 호출자가 고르는 것 | 경계 | Solo-API와의 관계 |
|---|---|---|---|---|
| REST | 원 시스템 | 자원 경로, 메서드, 본문 | API 계층 | 원 시스템의 API는 그대로 둔다. SA-501의 일은 REST로 남는다 |
| GraphQL | 원 시스템 | 필드와 관계 | 리졸버의 인가 | 비슷하게 "필요한 것만 가져간다". 차이는 질의를 호출자가 짜지 않고 등록된 문장만 쓴다는 점 |
| gRPC·JSON-RPC | 원 시스템 | 메서드와 메시지 | 서비스 계층 | 매퍼의 메서드가 RPC와 닮았다. 다만 네트워크 경계 없이 라이브러리로 부른다 |
| BFF (Backend for Frontend) | 소비자 팀 | 화면에 맞춘 엔드포인트 | BFF 서버 | 가장 가깝다. Solo-API는 AI 소비자를 위한 BFF를, 원 시스템 API가 아니라 DB 위에 직접 짓는다 |
| Text-to-SQL | 모델 | 자연어 | 모델과 DB 권한 | 정반대다. Solo-API는 모델이 SQL을 만들지 못하게 한다 (SA-101) |
| 범용 DB MCP 서버 | 도구 제작자 | 임의 질의 | DB 권한 | 쓰지 않는다. 임의 질의를 받는 범용 도구를 금지한다 (SA-306) |
| MCP | 도구 제작자 | 도구와 인자 | 서버 | 3장의 도구를 내놓는 규약으로 쓴다. MCP 자체는 권한 검사를 대신하지 않는다 |

## 쓰지 말아야 할 때

- **원 시스템 팀이 DB 직접 접근을 허락하지 않을 때.** 읽기 전용 역할조차 받을 수 없으면 Solo-API의 전제가 없다.
- **원 시스템의 쓰기 규칙이 서버 코드에 많이 숨어 있을 때.** 쓰기는 포기하고 읽기만 매퍼로 한다.
- **여러 조직이 같은 DB를 쓰고, 행 단위 권한이 서버에서만 판정될 때.** 계정 조건만으로 범위를 걸 수 없다.
- **소비자가 많고 서로 다른 언어일 때.** 매퍼를 각자 설치하기 어렵다면 매퍼 앞에 내부 API를 하나 두는 편이 낫다. 그것도 규칙은 그대로다.
- **외부 공개 API가 필요할 때.** Solo-API는 내부망의 AI 서비스를 위한 방식이다. 외부 소비자에게는 REST나 GraphQL로 낸다.

## 비슷한 방식을 소개하는 저장소들

API 방식을 규칙 문서와 구현으로 소개하는 저장소는 많다. 이 저장소의 구성(번호 붙은 규칙, MUST/SHOULD 표시, 참조 구현, 변경 이력)은 아래를 참고했다.

| 저장소 | 형식 |
|---|---|
| [zalando/restful-api-guidelines](https://github.com/zalando/restful-api-guidelines) | 번호 붙은 규칙과 MUST/SHOULD/MAY |
| [microsoft/api-guidelines](https://github.com/microsoft/api-guidelines) | 한 문서의 지침과 기여 절차 |
| [googleapis/aip](https://github.com/googleapis/aip) | 번호 붙은 개선 제안(AIP) |
| [json-api/json-api](https://github.com/json-api/json-api) | 규격 버전, FAQ, 구현 목록 |
| [graphql/graphql-spec](https://github.com/graphql/graphql-spec) | 규격 본문과 변경 이력 |
| [OAI/OpenAPI-Specification](https://github.com/OAI/OpenAPI-Specification) | 규격 버전, 구현 목록, 보안 고려 사항 |
