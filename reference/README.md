# Solo-API 참조 구현 (Python)

[SPEC.md](../SPEC.md)의 규칙을 가장 작은 형태로 옮긴 코드다. SQLite와 표준 라이브러리만 쓰고, MCP 어댑터만 공식 SDK가 필요하다.
운영용이 아니라 규칙을 읽고 시험하기 위한 코드다. 운영 구현은 PostgreSQL의 역할·잠금·시간 제한을 쓴다.

```bash
cd reference
pip install -e ".[mcp]"                         # MCP 어댑터까지 쓸 때. 나머지는 설치 없이도 돈다
python -m unittest discover -s tests            # 29개 (MCP SDK가 없으면 1개 건너뜀)

python -m solo.demo catalog anthropic           # 모델에 줄 도구 정의 (mcp | openai | anthropic | full)
python -m solo.demo call task_list '{}' --account acct-a
python -m solo.demo tick --account acct-a       # cron이 부르는 한 줄
SOLO_ACCOUNT_ID=acct-a python -m solo.mcpio     # MCP stdio 서버 (프로토콜 2026-07-28, mcp==2.3.0으로 확인)
```

## 구성

| 파일 | 역할 | 규격 |
|---|---|---|
| `solo/sqlreg.py` | 고정 SQL 등록부. 계정 파라미터가 없는 문장은 등록 단계에서 거절 | 1장 |
| `solo/mapper.py` | 읽기 매퍼. 등록된 문장만, 읽기 전용 연결, 값 없는 감사 기록 | 1장 |
| `solo/writer.py` | 쓰기 매퍼. 호출 1회 = 트랜잭션 1개, 버전 비교, 변경 전 값, `dry_run` | 2장 |
| `solo/tools.py` | 도구 등록부. 닫힌 입력, 응답 봉투, 재시도 안전, 카탈로그 내보내기 | 3장 |
| `solo/jobs.py` | 작업 상태 기계 | 4장 |
| `solo/demo.py` | 위를 엮은 할 일 서비스. HTTP 엔드포인트가 없다 | 전체 |
| `solo/mcpio.py` | 같은 도구를 MCP stdio로 내놓는 선택 어댑터 | 3장 |

## 적합성 표

"있음"은 그 규칙을 확인하는 시험이 있다는 뜻이다. "없음"은 참조 구현이 그 규칙을 구현하지 않았거나 시험하지 못한다는 뜻이고, 이유를 적었다.

| 규칙 | 시험 | 상태 |
|---|---|---|
| SA-101 등록된 문장만 | `test_map` `test_only_registered_read_statements_with_exact_parameters` | 있음 |
| SA-102 등록부 고정 | 등록부가 `MappingProxyType` | 시험 없음 |
| SA-103 계정 범위 | `test_registry_refuses_unscoped_statement` | 있음 |
| SA-104 관리용 문장 분리 | 참조 구현은 관리용 문장을 아예 등록하지 못한다 | 해당 없음 |
| SA-105 파라미터 집합 일치 | `test_only_registered_read_statements_with_exact_parameters` | 있음 |
| SA-106 읽기 전용 | 읽기 전용 연결(`mode=ro`). SQLite에는 DB 역할이 없다 | 일부 |
| SA-107 다른 계정 = 없는 행 | `test_other_account_looks_like_missing`, `test_read_is_scoped_to_the_host_account` | 있음 |
| SA-108 한 스냅샷 | 한 문장씩만 실행한다 | 없음 |
| SA-109 상한과 잘림 표시 | `test_list_is_capped_and_marked` | 있음 |
| SA-110 시간 제한, 동시 조회 제한 | SQLite에서 구현하지 않았다 | 없음 |
| SA-111 실패는 빈 결과가 아님 | `test_failure_is_never_an_empty_result` | 있음 |
| SA-112 복호화 실패 | 암호화 컬럼이 없다 | 해당 없음 |
| SA-113 감사 기록에 값 없음 | `test_audit_has_ids_and_counts_but_no_values` | 있음 |
| SA-114, SA-115 | 저장소 운영 규칙 | 해당 없음 |
| SA-201 쓰기 역할 분리 | SQLite에는 DB 역할이 없다 | 없음 |
| SA-202 설정 없으면 쓰기 거절 | 구현하지 않았다 | 없음 |
| SA-203 트랜잭션 하나, 잠금 | `BEGIN IMMEDIATE`로 DB 전체를 잠근다 | 일부 |
| SA-204 버전 충돌 | `test_version_conflict_before_image_and_undo` | 있음 |
| SA-205 변경 전 값, 되돌리기 | `test_version_conflict_before_image_and_undo` | 있음 |
| SA-206 `dry_run` | `test_dry_run_rolls_back` | 있음 |
| SA-207 ~ SA-209 | 원 시스템 서버와 암호화가 없다 | 해당 없음 |
| SA-301, SA-302 효과 선언 | `test_registration_rules`, `test_full_contract_and_generate_effect` | 있음 |
| SA-303 계정은 입력이 아님 | `test_registration_rules`, `test_invalid_input_is_refused_before_anything_runs`, `test_mcp` | 있음 |
| SA-304 닫힌 입력 | `test_invalid_input_is_refused_before_anything_runs` | 있음 |
| SA-305 Question 표시 | `test_catalog_formats_share_one_definition` | 있음 |
| SA-306 범용 도구 금지 | 링크 입력이 없다 | 해당 없음 |
| SA-307 요청 ID | `test_write_needs_request_id_and_replays_the_same_intent`, `test_concurrent_calls_with_one_request_id_write_once` | 있음 |
| SA-308 시간 창 | `test_content_window_for_clients_without_request_id` | 있음 |
| SA-309 응답 봉투와 오류 코드 | 여러 시험에서 코드와 `next_action` 확인 | 있음 |
| SA-310 결과 불명 재전송 금지 | `test_failure_is_never_an_empty_result`, `test_content_window_for_clients_without_request_id` | 있음 |
| SA-311 한 정의에서 카탈로그 | `test_catalog_formats_share_one_definition` (사람이 읽는 목록 생성은 없음) | 일부 |
| SA-312 보류 도구 | `test_failure_is_never_an_empty_result` | 있음 |
| SA-313 도구 감사 기록 | `test_audit_has_no_arguments` | 있음 |
| SA-401 작업 상태 | `test_jobs` 전체 | 있음 |
| SA-402 업무 키당 작업 하나 | `test_same_business_key_is_one_job` | 있음 |
| SA-403 원 시스템 확인 | `test_success_needs_the_original_system_to_confirm`, `test_lost_reply_after_save_is_confirmed_by_lookup` | 있음 |
| SA-404 이미 있으면 다시 하지 않음 | `test_already_processed_is_recorded_without_acting` | 있음 |
| SA-405 `unknown` 재실행 금지 | `test_unknown_is_not_rerun_and_is_reconciled_by_a_lookup` | 있음 |
| SA-406 임대 만료 | `test_worker_turn_expires_a_dead_workers_lease` | 있음 |
| SA-407 실패 재접수 | `test_failed_or_cancelled_key_runs_again_but_unknown_does_not` | 있음 |
| SA-408 미리보기·접수·승인 | 구현하지 않았다 | 없음 |
| SA-409 스케줄러 | `test_scheduler_firing_twice_writes_one_report`, `test_scheduler_reruns_a_day_whose_job_failed` | 있음 |
| SA-410 요청 주체와 실행 계정 | 실행 계정을 기록하지 않는다 | 없음 |
| SA-501 ~ SA-505 | 설계 규칙 | 해당 없음 |

**MUST 중 참조 구현이 지키지 못하는 것**: SA-110, SA-201, SA-202, SA-410.
SQLite와 단일 프로세스의 한계이거나 아직 만들지 않은 것이다. 운영 구현은 이 넷을 따로 갖춰야 한다.
