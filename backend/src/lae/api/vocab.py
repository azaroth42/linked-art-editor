"""``/api/vocab``: the vocabulary bundle and Value Set term search (Phase 2 Step 2.1; spec §7).

- ``GET /api/vocab/bundle``: every Value Set and binding (``lae.vocab.service.get_bundle``), with
  a weak ``ETag`` derived from the registry version and the installation's label language
  (``Settings.label_language``, which chooses the default label of a term with a curated
  override); ``If-None-Match`` is answered with 304.
  ``Cache-Control: private, no-cache`` makes the browser revalidate each time, which costs one
  query when nothing changed.
- ``GET /api/vocab/{id}/terms?q=&limit=``: the terms of one Value Set matching ``q``
  (``lae.vocab.service.search_terms``), labels in the caller's locale chain, default labels
  under ``Settings.label_language``. 404
  ``LAE-VOCAB-001`` for an unknown id.

Both need a signed-in user (``current_user``); any role may read the vocabularies.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from lae.api.deps import locale_chain
from lae.api.errors import ApiError
from lae.api.public import if_none_match
from lae.app import get_app_settings
from lae.auth import CurrentUser, current_user
from lae.db.session import get_session
from lae.logging import bind_log_context
from lae.settings import Settings
from lae.vocab.membership import UnknownVocabularyError
from lae.vocab.service import TermOption, VocabBundle, cached_bundle, search_terms

UNKNOWN_VOCABULARY: Final = "LAE-VOCAB-001"
"""404: no Value Set has this id; params ``vocabulary_id``."""

VOCAB_CODES: Final = (UNKNOWN_VOCABULARY,)
"""Every code this router emits (English text in ``frontend/public/locales/en/errors.json``)."""

DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 100
MAX_QUERY_LENGTH: Final = 200

CACHE_CONTROL: Final = "private, no-cache"

router = APIRouter(prefix="/api/vocab", tags=["vocab"])


@router.get(
    "/bundle",
    response_model=VocabBundle,
    responses={status.HTTP_304_NOT_MODIFIED: {"description": "If-None-Match matched"}},
)
async def vocab_bundle(
    request: Request,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Response:
    """Every Value Set (terms with resolved labels) and every binding, ETag-cached."""
    bind_log_context(user_id=user.id)
    cached = await cached_bundle(session, settings.label_language)
    headers = {"ETag": cached.etag, "Cache-Control": CACHE_CONTROL}
    if if_none_match(request.headers.get("if-none-match"), cached.etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(cached.body, media_type="application/json", headers=headers)


@router.get(
    "/{vocabulary_id}/terms",
    responses={status.HTTP_404_NOT_FOUND: {"description": "LAE-VOCAB-001"}},
)
async def vocab_terms(
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    chain: Annotated[list[str], Depends(locale_chain)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    vocabulary_id: Annotated[str, Path(description="Value Set id, e.g. vocab:gender")],
    q: Annotated[
        str, Query(max_length=MAX_QUERY_LENGTH, description="Label prefix; empty: shortlist")
    ] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[TermOption]:
    """Terms of a Value Set whose label starts with ``q``, most used first."""
    bind_log_context(user_id=user.id)
    try:
        return await search_terms(session, vocabulary_id, q, limit, chain, settings.label_language)
    except UnknownVocabularyError as error:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, UNKNOWN_VOCABULARY, {"vocabulary_id": error.vocabulary_id}
        ) from error
