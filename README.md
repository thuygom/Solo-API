# Solo-API

**AI 서비스가 백엔드 API를 요청하지 않고 업무 시스템과 일하는 방식.**
고정 SQL 매퍼로 업무 DB를 읽고 쓰고, 그 위에 업무 의도 단위의 도구(MCP)와 작업(RPA·스케줄러)을 올린다.

> Solo-API is a way for an AI service to work with a business system without asking its backend team for new APIs.
> It reads and writes the system's database only through a registry of reviewed SQL statements, and exposes intent-level tools (MCP and function calling) and jobs (RPA and schedulers) on top of it.
> See [SPEC.md](SPEC.md) (Korean) for the rules and [reference/](reference/) for a tested Python kernel.

상태: 규격 0.1 초안 · [SPEC.md](SPEC.md) · [참조 구현](reference/) · [라이선스 Apache-2.0](LICENSE)

## 왜

SaaS에 AI 기능을 붙이면 기능마다 백엔드 API가 하나씩 필요해진다. 명세·구현·배포·검수가 모두 백엔드 팀의 일이 되고, AI 쪽 필드 하나가 늘 때마다 그 일이 다시 생긴다.
백엔드 인력은 줄고 AI 기능은 늘어서, 요청이 쌓이는 속도가 처리 속도를 넘는다.

Solo-API는 요청의 수가 아니라 **요청의 단위**를 바꾼다.

- 데이터를 읽고 쓰는 일은 AI 서비스가 직접 한다. 단, 미리 검토해 등록한 SQL 문장만 실행한다.
- 저장할 자리가 없으면 API가 아니라 테이블·컬럼을 요청한다.
- API로 남아야 하는 일(토큰 발급, 파일 객체, 계정 간 권한)은 남긴다.

자세한 배경은 [docs/rationale.md](docs/rationale.md)에 있다.

## REST·GraphQL과 무엇이 다른가

Solo-API는 HTTP API의 모양을 정하는 규격이 아니다. **AI 서비스가 원 시스템에 무엇을 요청하지 않아도 되는지**를 정한다.

| | REST | GraphQL | Solo-API |
|---|---|---|---|
| 누가 만드나 | 원 시스템의 백엔드 | 원 시스템의 백엔드 | 데이터를 쓰는 AI 서비스 |
| 계약의 단위 | 자원과 메서드 | 스키마와 쿼리 | 등록된 SQL 문장, 업무 의도 단위 도구, 작업 |
| 호출자가 고르는 것 | 경로와 본문 | 필드 | 도구와 사용자만 아는 값 |
| 새 데이터가 필요할 때 | 엔드포인트 추가 요청 | 스키마 확장 요청 | 문장 하나 등록, 자리가 없으면 테이블·컬럼 요청 |
| 경계 | API 계층의 인증·인가 | 리졸버의 인가 | DB 역할, 고정 문장, 네트워크 |

REST나 GraphQL을 대신하지 않는다. 원 시스템은 계속 자기 API를 갖고, Solo-API는 그 옆에서 AI 쪽 요청을 줄인다.
비교와 "쓰지 말아야 할 때"는 [docs/compare.md](docs/compare.md)에 있다.

## 한눈에 보는 규칙

전체 규칙은 [SPEC.md](SPEC.md)에 번호를 붙여 두었다. 핵심은 다음과 같다.

1. **SQL은 코드에 고정한다.** 요청이나 모델 응답으로 SQL·테이블·컬럼·조건을 받지 않는다. (SA-101)
2. **계정은 서버가 정한다.** 모든 문장이 계정으로 범위를 걸고, 모델은 계정을 고를 수 없다. (SA-103, SA-303)
3. **모델은 사용자만 아는 값만 채운다.** 조회로 알 수 있는 값은 서버가 읽는다. (SA-304)
4. **쓰기는 되돌릴 수 있어야 한다.** 버전 비교, 변경 전 값, 롤백 미리보기, 의도당 요청 ID. (SA-204~206, SA-307)
5. **실패는 빈 결과가 아니다.** 결과를 모르면 다시 실행하지 않고 원 시스템에서 대조한다. (SA-111, SA-310, SA-405)
6. **API로 남길 것은 남긴다.** (SA-501)

## 모양

```
               사용자 ── AI 비서(모델) ── 도구 등록부 ──┐
                                                      ├─ 매퍼(고정 SQL) ── 업무 DB
               스케줄러(cron) ── 작업 상태 기계 ───────┤
                                                      └─ RPA·외부 API ─── 화면만 있는 시스템
               원 시스템 서버 ── (저장본 조회만)
```

```python
SQL = registry({
    "read.task": "SELECT id, title, status FROM task WHERE account_id = :account_id AND id = :task_id",
})
read = ReadMapper("app.db", SQL)
read.one("read.task", account_id=verified_account, task_id=task_id)   # 등록된 문장, 정확한 파라미터만
```

## 이 저장소

| 경로 | 내용 |
|---|---|
| [SPEC.md](SPEC.md) | 규격. 번호 붙은 규칙과 이유 |
| [docs/rationale.md](docs/rationale.md) | 왜 이 방식인가 |
| [docs/compare.md](docs/compare.md) | REST, GraphQL, BFF, RPC와의 비교와 쓰지 말아야 할 때 |
| [docs/adoption.md](docs/adoption.md) | 도입 순서와 작업표 |
| [docs/security.md](docs/security.md) | 보안 고려 사항 |
| [docs/case.md](docs/case.md) | 적용 사례 (B2B SaaS 한 곳) |
| [docs/faq.md](docs/faq.md) | 자주 묻는 질문 |
| [reference/](reference/) | Python 참조 구현과 적합성 시험 |
| [IMPLEMENTATIONS.md](IMPLEMENTATIONS.md) | 구현 목록 |
| [SECURITY.md](SECURITY.md) | 보안 문제를 알리는 방법 |
| [CHANGELOG.md](CHANGELOG.md) | 규격 변경 이력 |

## 기여

규칙의 추가·변경은 이슈로 먼저 제안한다. [CONTRIBUTING.md](CONTRIBUTING.md)를 참고한다.

## 라이선스

[Apache License 2.0](LICENSE). 저작권 표시는 [NOTICE](NOTICE)에 있다.
인용한 외부 자료(MCP 사양, RFC 등)의 권리는 각 원저작자에게 있다.
