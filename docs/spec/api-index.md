# API, route, tool and command index (generated)

Generated from the phase guides by `scripts/gen_api_index.py` (kept in the scratch tooling; regenerate after editing guides). Each entry names the step that introduces or extends it. The guide step is the specification for parameters and permissions.

## HTTP endpoints

| Item | Introduced / extended in |
| :-- | :-- |
| `DELETE /api/records/{id}` | Phase 2 Step 2.5 |
| `DELETE /api/vocab/{id}` | Phase 2 Step 2.4 |
| `GET /activity/stream/page/{n}` | Phase 2 Step 2.16 |
| `GET /activity/stream` | Phase 2 Step 2.16 |
| `GET /api/admin/analytics/export` | Phase 4 Step 4.16 |
| `GET /api/admin/backups` | Phase 2 Step 2.19 |
| `GET /api/admin/sidecars` | Phase 2 Step 2.14 |
| `GET /api/batches/{id}` | Phase 2 Step 2.8 |
| `GET /api/campaigns/{id}/next` | Phase 3 Step 3.10 |
| `GET /api/drafts` | Phase 1 Step 1.10, Phase 1 Step 1.15, Phase 2 Step 2.9 |
| `GET /api/explore/places?source=…&ref=…` | Phase 4 Step 4.4 |
| `GET /api/explore/timeline?source=…&ref=…` | Phase 4 Step 4.5 |
| `GET /api/graph/neighbors?uri=…&predicates=…&limit=25` | Phase 4 Step 4.2 |
| `GET /api/graph/path?from=…&to=…&max_hops=4` | Phase 4 Step 4.2 |
| `GET /api/jobs` | Phase 3 Step 3.7 |
| `GET /api/merge-batches?record_id=` | Phase 2 Step 2.6 |
| `GET /api/notifications?unread=1` | Phase 2 Step 2.9 |
| `GET /api/profiles/{class}` | Phase 1 Step 1.18 |
| `GET /api/proposals?status=needs_human_review` | Phase 3 Step 3.8 |
| `GET /api/records/{id}/backlinks` | Phase 2 Step 2.5 |
| `GET /api/records/{id}/reclassify/check?target_type=Group` | Phase 4 Step 4.13 |
| `GET /api/records/{id}/revisions/{m}/diff/{n}` | Phase 1 Step 1.14 |
| `GET /api/records/{id}/revisions/{n}` | Phase 1 Step 1.6, Phase 1 Step 1.14 |
| `GET /api/records/{id}/revisions` | Phase 1 Step 1.6, Phase 1 Step 1.14 |
| `GET /api/records/{id}/timeline` | Phase 4 Step 4.5 |
| `GET /api/records/{id}` | Phase 1 Step 1.6, Phase 1 Step 1.17 |
| `GET /api/search/autocomplete?q=&types=Person,Group&limit=20&locale=` | Phase 1 Step 1.13 |
| `GET /api/search/autocomplete` | Phase 3 Step 3.2 |
| `GET /api/search?q=&class=&facet.<name>=&page=` | Phase 2 Step 2.7 |
| `GET /api/smart-sets/{id}/sparql` | Phase 4 Step 4.6 |
| `GET /api/smart-sets/{id}` | Phase 4 Step 4.6 |
| `GET /api/sparql/completions` | Phase 4 Step 4.7 |
| `GET /api/templates?class=` | Phase 2 Step 2.10 |
| `GET /api/vocab/bundle` | Phase 1 Step 1.18, Phase 2 Step 2.1 |
| `GET /api/vocab/export` | Phase 2 Step 2.4 |
| `GET /api/vocab/{id}/terms?q=&limit=` | Phase 2 Step 2.1 |
| `GET /api/vocab/{id}/terms?q=` | Phase 2 Step 2.3 |
| `GET /api/vocab/{id}/usage` | Phase 2 Step 2.4 |
| `GET /auth/callback` | Phase 1 Step 1.7 |
| `GET /auth/login` | Phase 1 Step 1.7 |
| `GET /auth/me` | Phase 1 Step 1.7 |
| `GET /health/live` | Phase 1 Step 1.1, Phase 2 Step 2.17 |
| `GET /health/ready` | Phase 1 Step 1.1, Phase 2 Step 2.17 |
| `GET /id/{uuid}/node/{nid}` | Phase 1 Step 1.6 |
| `GET /id/{uuid}/timemap` | Phase 2 Step 2.16 |
| `GET /id/{uuid}` | Phase 1 Step 1.6 |
| `GET /internal/rooms/{id}/snapshot` | Phase 1 Step 1.8 |
| `GET /internal/rooms` | Phase 1 Step 1.8 |
| `GET /metrics` | Phase 2 Step 2.17 |
| `GET/POST /api/records/{id}/threads` | Phase 2 Step 2.9 |
| `GET/POST/PUT/DELETE /api/admin/llm-configs` | Phase 3 Step 3.12 |
| `GET/PUT /api/admin/agents/*` | Phase 3 Step 3.4 |
| `POST /api/admin/agents/pause` | Phase 3 Step 3.4 |
| `POST /api/admin/backups` | Phase 2 Step 2.19 |
| `POST /api/admin/daemons/{name}/run` | Phase 2 Step 2.17 |
| `POST /api/admin/outbox/{id}/retry|skip` | Phase 2 Step 2.17 |
| `POST /api/admin/qlever/rebuild` | Phase 2 Step 2.18 |
| `POST /api/admin/sidecars/{id}/load` | Phase 2 Step 2.14 |
| `POST /api/admin/sidecars/{id}/unload` | Phase 2 Step 2.14 |
| `POST /api/batches/find-replace` | Phase 2 Step 2.8 |
| `POST /api/batches/{id}/commit` | Phase 2 Step 2.8, Phase 2 Step 2.15 |
| `POST /api/batches/{id}/revert` | Phase 2 Step 2.8 |
| `POST /api/campaigns` | Phase 3 Step 3.10 |
| `POST /api/chat/{session}/confirm/{call_id}` | Phase 3 Step 3.13 |
| `POST /api/chat/{session}/messages` | Phase 3 Step 3.13 |
| `POST /api/chat/{session}/turns/{n}/revert` | Phase 3 Step 3.13 |
| `POST /api/explore/places/within` | Phase 4 Step 4.4 |
| `POST /api/jobs/{id}/cancel` | Phase 3 Step 3.7 |
| `POST /api/me/llm-keys` | Phase 3 Step 3.12 |
| `POST /api/merge-batches/{id}/unmerge` | Phase 2 Step 2.6 |
| `POST /api/notifications/{id}/read` | Phase 2 Step 2.9 |
| `POST /api/parse/prose` | Phase 4 Step 4.14 |
| `POST /api/proposals/{id}/approve|reject|unmerge` | Phase 3 Step 3.8 |
| `POST /api/public/{id}/translate?lang=…` | Phase 4 Step 4.11 |
| `POST /api/records/duplicate-check` | Phase 3 Step 3.2 |
| `POST /api/records/from-template/{name}` | Phase 2 Step 2.10 |
| `POST /api/records/{id}/clone` | Phase 2 Step 2.10 |
| `POST /api/records/{id}/draft/apply` | Phase 3 Step 3.6 |
| `POST /api/records/{id}/draft/diff` | Phase 1 Step 1.14, Phase 1 Step 1.19, Phase 4 Step 4.2 |
| `POST /api/records/{id}/draft/discard` | Phase 1 Step 1.10, Phase 2 Step 2.9 |
| `POST /api/records/{id}/draft/extend` | Phase 1 Step 1.15, Phase 2 Step 2.9 |
| `POST /api/records/{id}/draft/offline-diff` | Phase 4 Step 4.15 |
| `POST /api/records/{id}/draft/translate` | Phase 4 Step 4.10 |
| `POST /api/records/{id}/draft/validate` | Phase 1 Step 1.9, Phase 1 Step 1.19 |
| `POST /api/records/{id}/merge-into/{target_id}` | Phase 2 Step 2.6 |
| `POST /api/records/{id}/protect` | Phase 3 Step 3.8 |
| `POST /api/records/{id}/publish` | Phase 1 Step 1.10, Phase 2 Step 2.2 |
| `POST /api/records/{id}/reclassify` | Phase 4 Step 4.13 |
| `POST /api/records/{id}/revisions/{n}/revert` | Phase 1 Step 1.14 |
| `POST /api/records/{id}/split/execute` | Phase 4 Step 4.12 |
| `POST /api/records/{id}/split/plan` | Phase 4 Step 4.12 |
| `POST /api/records/{id}/unprotect` | Phase 3 Step 3.8 |
| `POST /api/records/{referrer_id}/draft/apply` | Phase 4 Step 4.14 |
| `POST /api/records/{uuid}/reconcile` | Phase 3 Step 3.11 |
| `POST /api/records` | Phase 1 Step 1.6, Phase 1 Step 1.7, Phase 1 Step 1.15, Phase 1 Step 1.17, Phase 1 Step 1.18 |
| `POST /api/search/resultset` | Phase 4 Step 4.1 |
| `POST /api/smart-sets/{id}/run` | Phase 4 Step 4.6 |
| `POST /api/smart-sets` | Phase 4 Step 4.6 |
| `POST /api/split-batches/{id}/unsplit` | Phase 4 Step 4.12 |
| `POST /api/threads/{id}/comments` | Phase 2 Step 2.9 |
| `POST /api/threads/{id}/resolve` | Phase 2 Step 2.9 |
| `POST /api/tokens` | Phase 1 Step 1.7 |
| `POST /api/validate` | Phase 1 Step 1.9 |
| `POST /api/vocab/import` | Phase 2 Step 2.4 |
| `POST /api/vocab/translate-missing?lang=fr` | Phase 4 Step 4.9 |
| `POST /api/vocab/{id}/refresh` | Phase 2 Step 2.4 |
| `POST /auth/logout` | Phase 1 Step 1.7 |
| `POST /internal/embed` | Phase 3 Step 3.1 |
| `POST /internal/rooms/{id}/apply` | Phase 1 Step 1.8 |
| `POST /internal/rooms/{id}/close` | Phase 1 Step 1.8 |
| `POST /internal/rooms/{id}/open` | Phase 1 Step 1.8 |
| `POST /internal/system/broadcast` | Phase 2 Step 2.17 |
| `POST /internal/users/{id}/eject` | Phase 1 Step 1.8 |
| `POST /reconcile` | Phase 3 Step 3.11 |
| `POST/GET /sparql/vector` | Phase 3 Step 3.3 |
| `POST/GET /sparql` | Phase 2 Step 2.17 |
| `PUT /api/vocab/bindings` | Phase 2 Step 2.4 |
| `PUT /api/vocab/{id}/terms` | Phase 2 Step 2.4 |
| `PUT /api/vocab/{id}` | Phase 2 Step 2.4 |
| `PUT/DELETE /api/watch/{kind}/{id}` | Phase 2 Step 2.9 |

## HTTP endpoints (method in guide text)

| Item | Introduced / extended in |
| :-- | :-- |
| `/api/drafts/{id}/...` | Phase 1 Step 1.10 |
| `/api/graph/neighbors` | Phase 4 Step 4.3 |
| `/api/jobs/{id}` | Phase 3 Step 3.7 |
| `/api/profiles/schema/{class}` | Phase 1 Step 1.19 |
| `/api/records/{id}/draft/...` | Phase 1 Step 1.10 |
| `/api/records/{id}/publish` | Phase 2 Step 2.8 |
| `/api/records` | Phase 1 Step 1.6, Phase 1 Step 1.7 |
| `/api/search/autocomplete` | Phase 1 Step 1.18, Phase 2 Step 2.7, Phase 2 Step 2.14, Phase 3 |
| `/api/search` | Phase 2 Step 2.17 |
| `/api/vocab/bundle` | Phase 2 Step 2.3 |

## Public dereference

| Item | Introduced / extended in |
| :-- | :-- |
| `/id/:uuid` | Phase 1 Step 1.22 |
| `/id/` | Phase 1 Step 1.21 |
| `/id/{uuid}?version=N` | Phase 1 Step 1.20 |
| `/id/{uuid}` | Phase 1 Step 1.6, Phase 1 Step 1.12, Phase 1 Step 1.19, Phase 1 Step 1.21, Phase 1 Step 1.22, Phase 2 Step 2.5, Phase 2 Step 2.6, Phase 2 Step 2.16, Phase 2 Step 2.17, Phase 2 Step 2.18 |

## WebSocket routes

| Item | Introduced / extended in |
| :-- | :-- |
| `/ws/` | Phase 1 Step 1.17 |
| `/ws/rooms/<id>` | Phase 1 Step 1.8 |
| `/ws/rooms/{id}` | Phase 1 Step 1.17 |
| `/ws/rooms/{record_id}` | Phase 1 Step 1.8 |
| `/ws/system` | Phase 2 Step 2.17 |

## Internal RPC (crdt-ws)

| Item | Introduced / extended in |
| :-- | :-- |
| `/internal/embed` | Phase 3 Step 3.1 |

## Other routes

| Item | Introduced / extended in |
| :-- | :-- |
| `/activity/stream` | Phase 2 Step 2.20, Phase 4 Step 4.17 |
| `/auth/me` | Phase 1 Step 1.16 |
| `/health/live` | Phase 1 Step 1.1 |
| `/health/ready` | Phase 1 Step 1.1, Phase 2 Step 2.20 |
| `/mcp` | Phase 3 Step 3.5, Phase 3 Step 3.13 |
| `/metrics` | Phase 1 Step 1.11, Phase 1 Step 1.22, Phase 2 Step 2.20 |
| `/sparql/vector` | Phase 3 Step 3.3, Phase 3 Step 3.9, Phase 3 Step 3.13, Phase 4 Step 4.7 |
| `/sparql` | Phase 0 Step 0.3, Phase 2 Step 2.17, Phase 2 Step 2.18, Phase 2 Step 2.20, Phase 4 Step 4.7 |

## MCP resources

| Item | Introduced / extended in |
| :-- | :-- |
| `linkedart://draft/{uuid}` | Phase 3 Step 3.5 |
| `linkedart://job/{id}` | Phase 3 Step 3.5, Phase 3 Step 3.7, Phase 4 Step 4.10 |
| `linkedart://profile/{class}` | Phase 3 Step 3.5 |
| `linkedart://proposal/{id}` | Phase 3 Step 3.5 |
| `linkedart://proposals/pending?type=merge` | Phase 3 Step 3.5 |
| `linkedart://qa/worklists` | Phase 3 Step 3.5, Phase 3 Step 3.10 |
| `linkedart://record/{uuid}` | Phase 3 Step 3.5 |
| `linkedart://shapes/{class}` | Phase 3 Step 3.5 |
| `linkedart://vocabularies` | Phase 3 Step 3.5 |
| `linkedart://vocabulary/{id}` | Phase 3 Step 3.5 |

## MCP tools

| Item | Introduced / extended in |
| :-- | :-- |
| `add_classification` | Phase 3 Step 3.6 |
| `add_comment` | Phase 3 Step 3.6 |
| `add_equivalent` | Phase 3 Step 3.6 |
| `add_statement` | Phase 3 Step 3.6 |
| `approve_merge` | Phase 3 Step 3.8 |
| `cancel_job` | Phase 3 Step 3.7 |
| `claim_finding` | Phase 3 Step 3.10 |
| `claim_proposal` | Phase 3 Step 3.8 |
| `create_draft_record` | Phase 3 Step 3.6 |
| `escalate_to_human` | Phase 3 Step 3.8 |
| `get_backlinks` | Phase 2 Step 2.5, Phase 3 Step 3.5 |
| `preview_semantic_diff` | Phase 3 Step 3.6 |
| `propose_merge` | Phase 3 Step 3.8 |
| `reconcile_entity` | Phase 3 Step 3.5 |
| `reject_merge` | Phase 3 Step 3.8 |
| `resolve_finding` | Phase 3 Step 3.10 |
| `run_sparql_readonly` | Phase 3 Step 3.5 |
| `search_entities` | Phase 3 Step 3.5 |
| `set_place_wkt` | Phase 3 Step 3.6 |
| `set_preferred_name` | Phase 3 Step 3.6 |
| `set_production` | Phase 3 Step 3.6 |
| `start_job` | Phase 3 Step 3.7 |
| `translate_record_fields` | Phase 4 Step 4.10 |
| `validate_draft` | Phase 3 Step 3.6 |

## CLI commands

| Item | Introduced / extended in |
| :-- | :-- |
| `la-i18n status` | Phase 4 Step 4.9 |
| `la-i18n translate` | Phase 4 Step 4.9 |
| `la-i18n validate` | Phase 4 Step 4.9, Phase 4 Step 4.19 |
| `lae agents create` | Phase 3 Step 3.4 |
| `lae bootstrap aat-labels` | Phase 1 Step 1.12 |
| `lae bootstrap build-index` | Phase 1 Step 1.12 |
| `lae bootstrap export-nt` | Phase 1 Step 1.12 |
| `lae bootstrap load` | Phase 1 Step 1.12 |
| `lae bootstrap rewrite-refs` | Phase 1 Step 1.12 |
| `lae embedder` | Phase 3 Step 3.1 |
| `lae embeddings migrate` | Phase 4 Step 4.18 |
| `lae embeddings swap` | Phase 4 Step 4.18 |
| `lae export loud` | Phase 2 Step 2.19 |
| `lae export` | Phase 4 Step 4.17 |
| `lae import csv` | Phase 2 Step 2.15 |
| `lae import jsonld` | Phase 2 Step 2.15 |
| `lae migrate` | Phase 0 Step 0.5, Phase 0 Step 0.12 |
| `lae reconcile-service` | Phase 3 Step 3.11 |
| `lae sidecar load` | Phase 2 Step 2.14 |
| `lae sync-worker` | Phase 1 Step 1.11 |
| `lae vocab export` | Phase 2 Step 2.1 |
| `lae vocab import` | Phase 2 Step 2.1, Phase 2 Step 2.2, Phase 2 Step 2.20 |
