<!-- Section numbers are preserved from the original design document (now in docs/old/design_doc.md). Guides cite these numbers as 'spec §N.N'. Do not renumber. -->

## 8. Security, Authentication & Open-Data Governance

### 8.1 Flexible Authentication & Single-Tenant Deployment

* **Interactive Users:** Standard OIDC / OAuth 2.0 integration (supporting institutional SSO/SAML bridges, GitHub, Google, ORCID, or local Keycloak) via Python `Authlib`.  
* **Programmatic Access:** Scoped Personal Access Tokens (PATs) and OAuth2 Client Credentials (service accounts) for ETL scripts, migration pipelines, and CI validation.  
* **Single-Tenant Deployment Model:** Each installation serves a single institution or community with one base URI namespace, one PostgreSQL database, one QLever instance, and one configuration set (OIDC providers, UI profiles, SHACL shapes). Running separate platforms is handled cleanly via separate native deployments rather than multi-tenant data partitioning.

### 8.2 Open Published Data & Write-Only Governance

* **Fully Open Published LOUD:** Once published, all records and fields are publicly readable Linked Open Usable Data—there are no private published records and no field-level redaction profiles.  
* **Write & Lifecycle Permissions (Tiered Human & Agent Governance):** Record-level and role-based ACLs govern authoring, curation, and review actions (`edit_draft`, `propose_merge`, `review_proposal`, `approve_merge_scoped`, `publish`, `delete`, `merge`, `unmerge`, `protect`/lock record, `manage_vocabularies`, `manage_roles`, `manage_users`, `manage_backups`, `manage_system`, `view_analytics`), mirroring Wikidata's page-protection model. System Admin and Ops administrative permissions are separated cleanly from editorial publish rights. AI agents authenticate as role-scoped service accounts across two tiers:  
  * *Tier 1 (Discovery / Triage Agents — small, low-cost models):* Hold `edit_draft` and `propose_merge` only; zero publish or destructive merge rights.  
  * *Tier 2 (Arbiter / Reviewer Agents — frontier models):* Hold `review_proposal` and policy-scoped `approve_merge_scoped`; may approve and auto-execute merges only within server-enforced blast-radius guardrails (neither record `protected`, source inbound QLever backlink count below a configurable threshold or an exact hard external identifier match such as identical ULAN/Wikidata/VIAF URI, and no contradictory core facts), reject false positives into the negative cache, or escalate uncertain, contradictory, protected, or high-blast-radius cases to human Editors (`needs_human_review`).  
  * *Human Editors:* Hold full `publish`, `merge`, `unmerge`, and `protect` authority, resolving escalated proposals in the Visual Merge Workbench and reversing any over-merge via 1-click Unmerge.  
  * *Interactive Chat Permission Intersection:* When a human instructs an agent through the Embedded Agent Chat Workbench, tool execution runs under the strict intersection of the invoking user's permissions and the selected agent tier (`effective_perms = user_perms ∩ agent_tier_perms`), preventing prompt-based privilege escalation (e.g., a Collaborator cannot instruct a frontier cloud model to publish or auto-approve a merge; Editor-initiated merges or multi-record agent jobs require explicit in-chat confirmation).  
* **Draft Boundary:** Unpublished drafts (including brand-new records not yet published) reside solely in PostgreSQL and never enter QLever until an editor publishes them.

### 8.3 Public QLever Access & Operational Protection

Because QLever indexes only fully open published data, no visibility graphs, secondary QLever instances, or SPARQL ACL query rewriting are required.

1. **Read-Only Public SPARQL & Autocomplete:** QLever serves autocomplete, backlinks, and open SPARQL queries directly behind a lightweight reverse proxy / FastAPI layer that enforces read-only methods (blocking external SPARQL Update), query timeouts, and basic rate limiting.  
2. **Internal Write Channel:** SPARQL 1.1 Update endpoints are bound exclusively to the internal network for the Python Sync Worker.
