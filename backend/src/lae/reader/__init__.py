"""The Public Reader: the server-rendered HTML page of a published record (Phase 1, Step 1.21).

``GET /id/{uuid}`` (``lae.api.public``) serves it when HTML is negotiated. It is Jinja2, with no
React (decision D8): ``view`` turns the public document into a view model, ``render`` fills
``templates/record.html``, ``i18n`` holds the strings (namespace ``reader``), ``dates`` formats
``TimeSpan``s (BCE included), ``iiif`` finds what Canvas Panel shows, and ``assets`` serves the
small script that loads Canvas Panel when there is something to show. Decisions: ADR-0031.
"""
